"""General browser interaction with observation-bound element handles."""

import asyncio
import base64
import json
import math
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from .browser_transfers import read_upload_payloads, regular_guest_file
from .observation import Projection, TextBudget
from .protocol import ActArguments, Lease, WorkerError

MAX_SCREENSHOT_BYTES = 8_000_000
INTERACTIVE = "button,input,textarea,select,a[href],[role],[tabindex],[contenteditable=true]"
ELEMENT_INFO = """el => {
  const normalized = v => String(v || '').replace(/\\s+/g, ' ').trim();
  const clean = v => normalized(v).slice(0, 300);
  const ids = (el.getAttribute('aria-labelledby') || '').split(/\\s+/);
  const named = ids.map(id => document.getElementById(id)?.textContent || '').join(' ');
  const label = el.getAttribute('aria-label') || named.trim() ||
    Array.from(el.labels || []).map(x => x.innerText).join(' ') || el.innerText ||
    el.getAttribute('placeholder') || el.getAttribute('title') || '';
  const r = el.getBoundingClientRect();
  const role = el.getAttribute('role') || ({BUTTON:'button', INPUT:el.type,
    TEXTAREA:'textbox', SELECT:'combobox', A:'link'}[el.tagName]) || el.tagName.toLowerCase();
  const result = {role, label:clean(label), disabled:!!el.disabled,
    focused:document.activeElement===el, bounds:{x:r.x,y:r.y,width:r.width,height:r.height}};
  if (normalized(label).length > 300) result.label_truncated = true;
  if (el.type === 'checkbox' || el.type === 'radio') result.checked = el.checked;
  else if ('value' in el && el.type !== 'password') {
    result.value = clean(el.value);
    if (normalized(el.value).length > 300) result.value_truncated = true;
  }
  return result;
}"""

# Retained as a private JSHandle, never part of model-visible observations.
# A coordinate is fenced by the observed node and every box overlapping that
# point. This catches replacement/overlays without coupling a button to clocks
# elsewhere on the page. This is DOM freshness, not pixel identity for canvas/video.
TARGET_SNAPSHOT = """({key, interactive}) => {
  const doc = document, root = document.documentElement;
  const parent = el => el.parentElement || el.getRootNode().host || null;
  const nodes = () => {
    const result = [];
    const visit = root => { for (const el of root.querySelectorAll('*')) {
      result.push(el); if (el.shadowRoot) visit(el.shadowRoot);
    }};
    visit(document); return result;
  };
  const rects = el => Array.from(el.getClientRects(), r => [r.x,r.y,r.width,r.height]);
  const style = el => {
    const s = getComputedStyle(el);
    return [s.display,s.visibility,s.opacity,s.pointerEvents,s.zIndex,s.transform,
      s.position,s.clipPath,s.overflow,s.filter,s.isolation];
  };
  const visible = (el, rs) => {
    const s = getComputedStyle(el);
    return el.isConnected && s.display !== 'none' && s.visibility === 'visible' &&
      s.opacity !== '0' && rs.some(([x,y,w,h]) => w>0 && h>0 && x<innerWidth &&
        y<innerHeight && x+w>0 && y+h>0);
  };
  const fingerprint = el => {
    const attrs = Array.from(el.attributes, a => [a.name,a.value]).sort();
    const names = (el.getAttribute('aria-labelledby') || '').split(/\\s+/)
      .map(id => document.getElementById(id)?.textContent || '');
    const labels = Array.from(el.labels || [], label => label.innerText);
    const text = el === document.body || el === document.documentElement ? '' :
      el.matches(interactive) ? el.innerText :
      Array.from(el.childNodes).filter(n => n.nodeType===Node.TEXT_NODE).map(n => n.textContent).join('');
    return JSON.stringify([el.tagName,attrs,names,labels,text,
      el.type === 'password' ? null : el.value,el.checked,el.matches(':disabled'),
      el.isContentEditable,el.form?.action,el.form?.method]);
  };
  const records = new Map();
  for (const el of nodes()) {
    const rs = rects(el);
    if (visible(el,rs)) records.set(el, {rects:rs,style:style(el),
      fingerprint:fingerprint(el),parent:parent(el)});
  }
  const focus = document.activeElement;
  const pointer = [...(window[key]?.pointer || [0,0])];
  const equal = (a,b) => JSON.stringify(a) === JSON.stringify(b);
  const same = el => {
    const old = records.get(el);
    return !!old && visible(el,rects(el)) && parent(el)===old.parent &&
      equal(rects(el),old.rects) && equal(style(el),old.style) &&
      fingerprint(el)===old.fingerprint;
  };
  const contains = (rs,x,y) => rs.some(([a,b,w,h]) => x>=a && y>=b && x<a+w && y<b+h);
  const deepHit = (x,y) => {
    let el=document.elementFromPoint(x,y), next;
    while (el?.shadowRoot && (next=el.shadowRoot.elementFromPoint(x,y)) && next!==el) el=next;
    return el;
  };
  const hitTarget = el => el?.closest(interactive) || el;
  const point = (x,y,expected=null) => {
    const hit=deepHit(x,y), target=expected || hitTarget(hit);
    let contained=hit;
    while (contained && contained!==target) contained=parent(contained);
    if (!target || !same(target) || contained!==target) return false;
    const prior=[...records].filter(([,r]) => contains(r.rects,x,y));
    const current=nodes().filter(el => {const rs=rects(el); return visible(el,rs) && contains(rs,x,y)});
    if (prior.length!==current.length) return false;
    return current.every((el,i) => {const old=records.get(el); return prior[i][0]===el && old &&
      contains(old.rects,x,y) && equal(style(el),old.style)});
  };
  const center = el => {
    const r=records.get(el)?.rects[0]; if (!r) return null;
    const [x,y,w,h]=r;
    return [(Math.max(0,x)+Math.min(innerWidth,x+w))/2,
      (Math.max(0,y)+Math.min(innerHeight,y+h))/2];
  };
  const order = () => nodes().filter(el => el.matches(interactive) &&
    visible(el,rects(el)) && !el.matches(':disabled'));
  const navigation = order();
  return {
    pointer,
    refsStable: refs => doc===document && root===document.documentElement && refs.every(same),
    check: action => {
      if (doc!==document || root!==document.documentElement) return false;
      if (action.target) {
        const p=center(action.target);
        if (!p || !point(...p,action.target)) return false;
      } else if (action.x != null) {
        if (!point(action.x,action.y)) return false;
      } else if (action.type==='scroll') {
        if (!equal(pointer,window[key]?.pointer || [0,0]) || !point(...pointer)) return false;
      } else {
        if (document.activeElement!==focus || !same(focus)) return false;
      }
      if (action.type==='key' && /(^|\\+)Tab$/.test(action.key)) {
        const current=order();
        if (current.length!==navigation.length || current.some((el,i)=>el!==navigation[i] || !same(el))) return false;
      }
      return action.type!=='drag' || point(action.end_x,action.end_y);
    }
  };
}"""


def text_block(value: dict) -> dict:
    return {"type": "text", "text": json.dumps(value, ensure_ascii=True)}


def validate_url(url: str):
    try:
        parts = urlsplit(url)
        valid = (parts.scheme in {"http", "https"} and bool(parts.hostname)) or url == "about:blank"
    except ValueError:
        valid = False
    if not valid or any(ord(char) < 32 for char in url):
        raise WorkerError("invalid_url", "Use an http(s) URL or about:blank", 422)


class Browser:
    def __init__(self, profile: Path, headless: bool = True):
        self.profile, self.headless = profile, headless
        self.playwright = None
        self.context = None
        self.page = None
        self.tabs = {}
        self.refs = {}
        self.observation_id = ""
        self.observation_signature = None
        self.observation_context_signature = None
        self.target_snapshot = None
        self.lease: Lease | None = None
        self.observer_key = "__meerkat_observation_" + secrets.token_hex(12)
        self.metric_sink = None
        self.persists_metrics = True
        self.operation_metrics = {}
        self.secure = None
        self.downloads_directory = self.profile.parent / "downloads"
        self.download_tasks = set()
        self.completed_downloads = []
        self.prepared_upload = None

    def record(self, name: str, amount: int = 1):
        self.operation_metrics[name] = self.operation_metrics.get(name, 0) + amount
        if self.metric_sink:
            self.metric_sink(name, amount)

    async def start(self):
        self.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.downloads_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.profile),
                headless=self.headless,
                viewport={"width": 1280, "height": 800} if self.headless else None,
                no_viewport=not self.headless,
                args=[]
                if self.headless
                else ["--force-renderer-accessibility", "--start-maximized"],
                accept_downloads=True,
                downloads_path=str(self.downloads_directory),
                chromium_sandbox=True,
            )
            self.context.set_default_timeout(5000)
            self.context.set_default_navigation_timeout(30000)
            self.context.on("page", self.track_downloads)
            for page in self.context.pages:
                self.track_downloads(page)
            self.page = (
                self.context.pages[0] if self.context.pages else await self.context.new_page()
            )
            self.sync_tabs()
        except BaseException:
            await self.playwright.stop()
            self.playwright = None
            raise

    def track_downloads(self, page):
        page.on("download", self.download_started)

    def download_started(self, download):
        task = asyncio.create_task(self.save_download(download))
        self.download_tasks.add(task)
        task.add_done_callback(self.download_tasks.discard)

    async def save_download(self, download):
        # Guest downloads are ordinary files. Do not trust a site's suggested
        # filename as a path, overwrite another download, or publish partial data.
        name = download.suggested_filename.replace("\\", "/").rsplit("/", 1)[-1]
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name).replace("..", "_").strip(".")[:180]
        name = name or "download"
        suffix = Path(name).suffix
        stem = name.removesuffix(suffix) if suffix else name
        path = self.downloads_directory / f"{stem}-{secrets.token_hex(8)}{suffix}"
        partial = path.with_name("." + path.name + ".partial")
        try:
            await download.save_as(partial)
            partial.replace(path)
            self.completed_downloads.append({"filename": path.name, "path": str(path)})
            self.completed_downloads = self.completed_downloads[-20:]
        except (PlaywrightError, OSError):
            # Closing a context cancels unfinished downloads. Never retain or
            # publish page-controlled error text or label a partial file complete.
            pass
        finally:
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass

    async def close(self):
        await self.invalidate()
        if self.context:
            await self.context.close()
        tasks = tuple(self.download_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.playwright:
            await self.playwright.stop()
        self.context = self.playwright = self.page = None

    def ready(self) -> bool:
        return self.context is not None and self.page is not None and not self.page.is_closed()

    async def configure(self, lease: Lease):
        changed = self.lease is None or (lease.lease_id, lease.epoch) != (
            self.lease.lease_id,
            self.lease.epoch,
        )
        if changed:
            await self.invalidate()
            for page in self.context.pages if self.headless else []:
                await page.set_viewport_size(
                    {"width": lease.observation.width, "height": lease.observation.height}
                )
        self.lease = lease

    def sync_tabs(self):
        self.tabs = {key: page for key, page in self.tabs.items() if not page.is_closed()}
        for page in self.context.pages:
            if page not in self.tabs.values():
                self.tabs["tab_" + secrets.token_hex(8)] = page
        if self.page is not None and self.page.is_closed():
            self.page = next(iter(self.tabs.values()), None)

    def current(self):
        self.sync_tabs()
        if self.page is None:
            raise WorkerError("no_active_tab", "Open a browser tab first")
        return self.page

    async def invalidate(self):
        self.prepared_upload = None
        self.observation_id = ""
        self.observation_signature = None
        self.observation_context_signature = None
        if self.target_snapshot is not None:
            try:
                await self.target_snapshot.dispose()
            except PlaywrightError:
                pass
            self.target_snapshot = None
        for handle in self.refs.values():
            try:
                await handle.dispose()
            except PlaywrightError:
                # A closed page already released its remote handles.
                continue
        self.refs = {}

    async def signature(self):
        page = self.current()
        signature = await page.evaluate(
            """([key,token]) => {
            if (!window[key]) {
                const state = {n:0,token,root:document.documentElement,pointer:[0,0]};
                new MutationObserver(() => state.n++).observe(document, {
                    subtree:true,childList:true,attributes:true,characterData:true});
                document.addEventListener('pointermove', e => {state.pointer=[e.clientX,e.clientY]}, true);
                window[key] = state;
            }
            if (window[key].root!==document.documentElement) {
                window[key].root=document.documentElement;window[key].token=token;
            }
            return [window[key].n,window[key].token, location.href, scrollX, scrollY, innerWidth, innerHeight];
        }""",
            [self.observer_key, secrets.token_hex(16)],
        )
        return (id(page), signature)

    async def context_signature(self):
        page, values = await self.signature()
        return page, values[1:]

    async def validate_observation(self, observation_id):
        """Validate ordinary observation context without admitting or consuming input."""
        if observation_id != self.observation_id or not self.observation_id:
            raise WorkerError("stale_observation", "Observe before acting")
        if await self.context_signature() != self.observation_context_signature:
            await self.invalidate()
            raise WorkerError("stale_observation", "Page changed since observation; observe again")

    def projection(self, arguments=None):
        if arguments is not None and arguments.scope_ref is not None:
            raise WorkerError(
                "unsupported_observation_scope", "scope_ref expands native desktop refs only", 422
            )
        return Projection.resolve(self.lease.observation, arguments)

    async def observe(self, arguments=None) -> list[dict]:
        if self.secure is not None:
            guarded = await self.secure.observation()
            if guarded is not None:
                return guarded
        projection = self.projection(arguments)
        self.record("observation_attempts")
        page = self.current()
        observation = self.lease.observation
        await self.invalidate()
        for _ in range(3):
            targets = None
            pending_handles = set()
            try:
                before = await self.context_signature()
                targets = await page.evaluate_handle(
                    TARGET_SNAPSHOT,
                    {
                        "key": self.observer_key,
                        "interactive": INTERACTIVE,
                    },
                )
                size = await page.evaluate("({width:innerWidth,height:innerHeight})")
                body = {
                    "observation_id": secrets.token_hex(16),
                    "surface": "browser",
                    "coordinate_space": "viewport",
                    "mode": observation.mode,
                    "downloads_directory": str(self.downloads_directory),
                    "downloads": list(self.completed_downloads),
                    **size,
                    "tab_id": next(key for key, item in self.tabs.items() if item is page),
                }
                new_refs = {}
                if observation.mode in {"semantic", "hybrid"}:
                    snapshot = await page.locator("body").aria_snapshot()
                    elements = []
                    inspected = 0
                    budget = TextBudget(projection.max_text_chars)
                    handles = await page.query_selector_all(INTERACTIVE)
                    pending_handles.update(handles)
                    for handle in handles:
                        if await handle.is_visible():
                            bounds = await handle.bounding_box()
                            if (
                                bounds
                                and bounds["x"] < size["width"]
                                and bounds["y"] < size["height"]
                                and bounds["x"] + bounds["width"] > 0
                                and bounds["y"] + bounds["height"] > 0
                            ):
                                inspected += 1
                                if len(elements) < projection.max_elements:
                                    ref = "ref_" + secrets.token_hex(8)
                                    element = {"ref": ref, **await handle.evaluate(ELEMENT_INFO)}
                                    for field in ("role", "label", "value"):
                                        if field in element:
                                            original = element[field]
                                            previously_truncated = element.get(
                                                field + "_truncated", False
                                            )
                                            count = budget.truncated_fields
                                            element[field] = budget.take(
                                                original,
                                                limit=160
                                                if projection.detail == "compact"
                                                else 300,
                                            )
                                            if element[field] != original:
                                                element[field + "_truncated"] = True
                                            if (
                                                previously_truncated
                                                and budget.truncated_fields == count
                                            ):
                                                budget.truncated_fields += 1
                                    elements.append(element)
                                    new_refs[ref] = handle
                                    continue
                        await handle.dispose()
                        pending_handles.discard(handle)
                    lines = []
                    for line in snapshot.splitlines(keepends=True):
                        if len(line) > budget.limit - budget.used:
                            budget.truncated_fields += 1
                            break
                        lines.append(budget.take(line))
                    accessibility = "".join(lines)
                    metadata = projection.metadata(inspected, len(elements), budget)
                    body.update(
                        url=page.url,
                        accessibility=accessibility,
                        accessibility_truncated=accessibility != snapshot,
                        elements=elements,
                        projection=metadata,
                        truncated=metadata["truncated"],
                    )
                blocks = [text_block(body)]
                if observation.mode in {"visual", "hybrid"}:
                    # Hiding carets mutates input styles, invalidating our own DOM
                    # observation fence. Capture without temporary page mutations.
                    screenshot = await page.screenshot(type="png", timeout=5000, caret="initial")
                    if len(screenshot) > MAX_SCREENSHOT_BYTES:
                        raise WorkerError(
                            "observation_too_large", "Screenshot exceeds the worker limit"
                        )
                    blocks.append(
                        {
                            "type": "image",
                            "data": base64.b64encode(screenshot).decode("ascii"),
                            "mimeType": "image/png",
                        }
                    )
                after = await self.context_signature()
                stable_refs = await targets.evaluate(
                    "(state, refs) => state.refsStable(refs)", list(new_refs.values())
                )
                if before == after and stable_refs:
                    signature = await self.signature()
                    self.observation_id = body["observation_id"]
                    self.observation_signature = signature
                    self.observation_context_signature, self.refs = after, new_refs
                    self.target_snapshot = targets
                    self.viewport_size = size
                    self.record("observations")
                    pending_handles.difference_update(new_refs.values())
                    targets = None  # Ownership transferred to the current observation.
                    return blocks
            finally:
                # Failed or cancelled captures never publish their remote handles.
                # Include the unvisited query tail, and preserve the original error
                # if the page closes while cleanup is running.
                if targets is not None:
                    pending_handles.add(targets)
                if pending_handles:
                    await asyncio.shield(
                        asyncio.gather(
                            *(handle.dispose() for handle in pending_handles),
                            return_exceptions=True,
                        )
                    )
        raise WorkerError("observation_unstable", "Page changed during capture; observe again")

    def point(self, x, y):
        size = getattr(
            self,
            "viewport_size",
            {"width": self.lease.observation.width, "height": self.lease.observation.height},
        )
        if (
            x is None
            or y is None
            or not math.isfinite(x)
            or not math.isfinite(y)
            or not (0 <= x < size["width"] and 0 <= y < size["height"])
        ):
            raise WorkerError("invalid_coordinates", "Coordinates must be inside the viewport", 422)
        return x, y

    async def validate(self, name: str, arguments):
        if name == "browser_navigate":
            validate_url(arguments.url)
            self.current()
        elif name == "browser_tabs":
            self.sync_tabs()
            if arguments.action in {"select", "close"} and arguments.tab_id not in self.tabs:
                raise WorkerError("unknown_tab", "Tab does not exist", 422)
            if arguments.url:
                validate_url(arguments.url)
        elif name == "computer_observe":
            self.current()
            self.projection(arguments)
        elif name == "computer_act":
            await self.validate_action(arguments)
        elif name == "browser_upload":
            await self.validate_upload(arguments)

    async def upload_target(self, arguments):
        if self.secure is not None and await self.secure.guarded():
            raise WorkerError("privacy_guarded", "Uploads are withheld during secure input")
        if self.lease is None or "browser_semantics" not in self.lease.capabilities:
            raise WorkerError("capability_denied", "Upload requires browser semantic refs", 403)
        await self.validate_observation(arguments.observation_id)
        handle = self.refs.get(arguments.ref)
        if handle is None:
            raise WorkerError("stale_ref", "Reference is not in the current observation")
        facts = await handle.evaluate("""el => ({connected:el.isConnected,
            file:el.tagName==='INPUT' && el.type==='file', disabled:el.matches(':disabled'),
            multiple:!!el.multiple, directory:!!el.webkitdirectory})""")
        if not facts["connected"]:
            raise WorkerError("stale_ref", "File input was detached")
        if not facts["file"] or facts["disabled"] or facts["directory"]:
            raise WorkerError("invalid_upload_target", "Upload requires an enabled file input", 422)
        if len(arguments.paths) > 1 and not facts["multiple"]:
            raise WorkerError("invalid_upload_target", "File input accepts only one file", 422)
        if self.target_snapshot is None or not await self.target_snapshot.evaluate(
            "(state, target) => state.check({type:'upload',target})", handle
        ):
            await self.invalidate()
            raise WorkerError("stale_observation", "File input changed; observe again")
        return handle

    @staticmethod
    def upload_key(arguments):
        return arguments.observation_id, arguments.ref, tuple(arguments.paths)

    async def validate_upload(self, arguments):
        self.prepared_upload = None
        await self.upload_target(arguments)
        payloads = await asyncio.to_thread(read_upload_payloads, arguments.paths)
        await self.upload_target(arguments)
        self.prepared_upload = self.upload_key(arguments), payloads

    def list_downloads(self):
        downloads = []
        for item in self.completed_downloads[-20:]:
            try:
                with regular_guest_file(item["path"]) as descriptor:
                    size = os.fstat(descriptor).st_size
            except WorkerError:
                continue
            downloads.append({**item, "size_bytes": size})
        return {
            "downloads_directory": str(self.downloads_directory),
            "downloads": downloads,
            "pending_count": sum(not task.done() for task in self.download_tasks),
        }

    async def upload(self, arguments):
        try:
            if self.prepared_upload is None or self.prepared_upload[0] != self.upload_key(
                arguments
            ):
                raise WorkerError("upload_not_prepared", "Validate the upload before acting")
            payloads = self.prepared_upload[1]
            handle = await self.upload_target(arguments)
            # No path is reopened after validation. Consume before the browser can
            # trigger a page-controlled input/change handler or network request.
            self.observation_id = ""
            self.record("upload_attempts")
            self.record("ui_command_attempts")
            await handle.set_input_files(payloads)
            self.record("uploads")
            self.record("ui_commands")
            return await self.observe(), self.operation_metrics
        finally:
            self.prepared_upload = None

    async def validate_action(self, arguments: ActArguments):
        action = arguments.action
        if action.path is not None:
            raise WorkerError(
                "unsupported_action", "Waypoint drag paths require the desktop surface", 422
            )
        guarded = self.secure is not None and await self.secure.guarded()
        if guarded and (action.type not in {"click", "focus"} or not action.ref):
            raise WorkerError(
                "privacy_guarded", "Use an already observed action ref or navigate away"
            )
        if action.type == "invoke":
            raise WorkerError(
                "unsupported_action",
                "Invoke uses desktop advertised actions; use click on browser refs",
                422,
            )
        if guarded:
            # Private-input continuations deliberately retain the existing strict
            # signature and already observed noneditable refs. No private field
            # fingerprint or fresh content is published while the guard is active.
            if arguments.observation_id != self.observation_id or not self.observation_id:
                raise WorkerError("stale_observation", "Observe before acting")
            if await self.signature() != self.observation_signature:
                await self.invalidate()
                raise WorkerError(
                    "stale_observation", "Page changed since observation; observe again"
                )
        else:
            await self.validate_observation(arguments.observation_id)
        if action.ref and action.ref not in self.refs:
            raise WorkerError("stale_ref", "Reference is not in the current observation")
        if action.x is not None:
            self.point(action.x, action.y)
        if action.type == "drag":
            self.point(action.end_x, action.end_y)
        if action.key:
            # Playwright supports modifiers plus a named key or one character.
            parts = action.key.split("+")
            modifiers = {"Alt", "Control", "ControlOrMeta", "Meta", "Shift"}
            keys = {
                "Enter",
                "Tab",
                "Escape",
                "Backspace",
                "Delete",
                "Space",
                "ArrowUp",
                "ArrowDown",
                "ArrowLeft",
                "ArrowRight",
                "Home",
                "End",
                "PageUp",
                "PageDown",
                "Insert",
            }
            if any(part not in modifiers for part in parts[:-1]) or not (
                len(parts[-1]) == 1
                or parts[-1] in keys
                or re.fullmatch(r"F(?:[1-9]|1[0-9]|2[0-4])", parts[-1])
            ):
                raise WorkerError("invalid_key", "Unsupported key combination", 422)
        if action.ref:
            handle = self.refs[action.ref]
            if not await handle.evaluate("el => el.isConnected"):
                raise WorkerError("stale_ref", "Element was detached")
            if action.type == "fill" and not await handle.is_editable():
                raise WorkerError("not_editable", "Fill requires an editable element", 422)
        if not guarded:
            target = {
                **action.model_dump(exclude_none=True),
                "target": self.refs.get(action.ref),
            }
            if self.target_snapshot is None or not await self.target_snapshot.evaluate(
                "(state, action) => state.check(action)", target
            ):
                await self.invalidate()
                raise WorkerError(
                    "stale_observation", "Target changed since observation; observe again"
                )

    async def perform(self, name: str, arguments) -> tuple[list[dict], dict]:
        self.operation_metrics = {}
        if name == "computer_observe":
            return await self.observe(arguments), self.operation_metrics
        if name == "computer_act":
            return await self.act(arguments)
        if name == "browser_upload":
            return await self.upload(arguments)
        if name == "browser_downloads":
            return [text_block(self.list_downloads())], self.operation_metrics
        if name == "browser_navigate":
            await self.invalidate()
            self.record("navigation_attempts")
            await self.current().goto(arguments.url, wait_until="domcontentloaded")
            self.record("navigations")
            return await self.observe(), self.operation_metrics
        return await self.manage_tabs(arguments)

    async def act(self, arguments: ActArguments) -> tuple[list[dict], dict]:
        page, action = self.current(), arguments.action
        handle = self.refs.get(action.ref)
        point = self.point(action.x, action.y) if action.x is not None else None
        # Consume before input. Keep the handle until the action finishes.
        self.observation_id = ""
        self.record("ui_command_attempts")
        self.record("clicks", 0)
        if action.type == "click":
            if handle:
                await handle.click(button=action.button, click_count=action.click_count)
            else:
                await page.mouse.click(*point, button=action.button, click_count=action.click_count)
            self.record("clicks", action.click_count)
        elif action.type == "focus":
            await handle.focus()
        elif action.type == "fill" and handle:
            await handle.fill(action.text)
        elif action.type in {"fill", "type", "key", "scroll"}:
            if handle:
                if action.type == "scroll":
                    await handle.hover()
                else:
                    await handle.focus()
            elif point:
                if action.type == "scroll":
                    await page.mouse.move(*point)
                else:
                    await page.mouse.click(*point)
                    self.record("clicks")
            if action.type == "fill":
                await page.keyboard.press("ControlOrMeta+A")
                await page.keyboard.insert_text(action.text)
            elif action.type == "type":
                await page.keyboard.insert_text(action.text)
            elif action.type == "key":
                await page.keyboard.press(action.key)
            else:
                if not handle and not point:
                    # A new document has no pointer event history, while
                    # Playwright can retain an old mouse position. Scroll at
                    # the position this observation actually validated.
                    await page.mouse.move(
                        *(await self.target_snapshot.evaluate("state => state.pointer"))
                    )
                await page.mouse.wheel(action.delta_x, action.delta_y)
        elif action.type == "drag":
            if handle:
                bounds = await handle.bounding_box()
                if not bounds:
                    raise WorkerError(
                        "action_effect_unknown", "Drag target disappeared", effect="unknown"
                    )
                point = (bounds["x"] + bounds["width"] / 2, bounds["y"] + bounds["height"] / 2)
            await page.mouse.move(*point)
            await page.mouse.down(button=action.button)
            try:
                await page.mouse.move(*self.point(action.end_x, action.end_y), steps=8)
            finally:
                await page.mouse.up(button=action.button)
        self.record("ui_commands")
        return await self.observe(), self.operation_metrics

    async def manage_tabs(self, arguments) -> tuple[list[dict], dict]:
        self.sync_tabs()
        if arguments.action == "list":
            if self.secure is not None and await self.secure.guarded():
                return await self.secure.observation(), self.operation_metrics
            return [
                text_block(
                    {
                        "tabs": [
                            {"tab_id": key, "url": page.url, "active": page is self.page}
                            for key, page in self.tabs.items()
                        ]
                    }
                )
            ], self.operation_metrics
        await self.invalidate()
        self.record("tab_command_attempts")
        if arguments.action == "new":
            self.page = await self.context.new_page()
            if self.headless:
                await self.page.set_viewport_size(
                    {"width": self.lease.observation.width, "height": self.lease.observation.height}
                )
            if arguments.url:
                await self.page.goto(arguments.url, wait_until="domcontentloaded")
        elif arguments.action == "select":
            self.page = self.tabs[arguments.tab_id]
            await self.page.bring_to_front()
        else:
            target = self.tabs.pop(arguments.tab_id)
            await target.close()
            if self.page is target:
                self.page = next(iter(self.tabs.values()), None)
            if self.page is None:
                self.page = await self.context.new_page()
                if self.headless:
                    await self.page.set_viewport_size(
                        {
                            "width": self.lease.observation.width,
                            "height": self.lease.observation.height,
                        }
                    )
        self.sync_tabs()
        self.record("tab_commands")
        return await self.observe(), self.operation_metrics
