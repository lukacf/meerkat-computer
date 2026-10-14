# Screenshot provenance

These are direct captures of running interfaces, without compositing or pixel
edits. They show scripted demonstrations, not autonomous model benchmark results.

- `console-preview.png` (1144 x 747, 2026-10-12): a direct browser capture of the
  inline App in the native MobKit Console. The installed Computer 0.1.0 wheel
  navigated to Workflow Bench scenario 17 through the real broker and worker.
  `computer_act` selected the NORTH station using an observed element reference;
  a fresh observation captured the field-record form. The main image is captured
  from the App's iframe to keep its content readable at README width.
- `console-context.png` (1280 x 1100, 2026-10-12): the full Console view from the
  same run. Sidebar, signals rail and tool details were collapsed with their
  normal UI controls. The fixture's landing route redirected to the public
  benchmark; tool responses and screenshots came from the actual worker.
  This is a local process fixture, not deployed HomeCore or VM qualification.
  Capture used the verified broker from Computer `08167f7`, MobKit `bc18a84e`,
  and Core `ac411ddf` with no new builds.
- `blender-python.png` (1280 x 720, 2026-10-10): Blender 4.0.2 on the real Linux
  desktop worker. The smoke test writes a Python script through `workspace_write`,
  executes it in Blender's Python console through `computer_act`, and verifies
  the saved scene and screenshot. The visible cube was renamed, scaled, and
  rotated by that script.

Local source evidence is retained under
`meerkat-computer-readme/.state/readme-showcase-20261012/north/run` and
`meerkat-computer/.state/release-observations-20261010/linux-current`.
The latter includes the Blender scene receipt and passing desktop report.
