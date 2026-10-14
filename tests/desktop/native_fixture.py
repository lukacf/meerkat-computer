"""Native GTK3 fixture for Linux desktop qualification, not a browser imitation."""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk


class NativeFixture(Gtk.Window):
    def __init__(self):
        super().__init__(title="Meerkat Native Fixture")
        self.set_default_size(640, 420)
        self.move(40, 40)
        self.connect("destroy", Gtk.main_quit)
        self.count = 0
        self.dialog_count = 0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(box)

        bar = Gtk.MenuBar()
        menu = Gtk.Menu()
        fixture = Gtk.MenuItem(label="Fixture")
        fixture.set_submenu(menu)
        open_dialog = Gtk.MenuItem(label="Open native dialog")
        open_dialog.connect("activate", self.open_dialog)
        menu.append(open_dialog)
        quit_item = Gtk.MenuItem(label="Quit fixture")
        quit_item.connect("activate", lambda *_: self.destroy())
        menu.append(quit_item)
        bar.append(fixture)
        box.pack_start(bar, False, False, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=18)
        box.pack_start(content, True, True, 0)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("A native editable text field")
        self.entry.get_accessible().set_name("Native message")
        content.pack_start(self.entry, False, False, 0)
        apply_button = Gtk.Button(label="Apply message")
        apply_button.connect("clicked", self.apply_message)
        content.pack_start(apply_button, False, False, 0)
        self.message = Gtk.Label(label="Message: empty")
        content.pack_start(self.message, False, False, 0)
        increment = Gtk.Button(label="Increment native count")
        increment.connect("clicked", self.increment)
        content.pack_start(increment, False, False, 0)
        self.counter = Gtk.Label(label="Count: 0")
        content.pack_start(self.counter, False, False, 0)
        self.dialog_status = Gtk.Label(label="Dialogs confirmed: 0")
        content.pack_start(self.dialog_status, False, False, 0)

    def apply_message(self, *_):
        self.message.set_text("Message: " + self.entry.get_text())

    def increment(self, *_):
        self.count += 1
        self.counter.set_text(f"Count: {self.count}")

    def open_dialog(self, *_):
        dialog = Gtk.Dialog(title="Native Details", transient_for=self, modal=True)
        dialog.set_default_size(360, 180)
        dialog.add_button("Confirm native dialog", Gtk.ResponseType.OK)
        label = Gtk.Label(label="This is a second native GTK window.", margin=20)
        dialog.get_content_area().add(label)

        def finish(window, response):
            if response == Gtk.ResponseType.OK:
                self.dialog_count += 1
                self.dialog_status.set_text(f"Dialogs confirmed: {self.dialog_count}")
            window.destroy()

        dialog.connect("response", finish)
        dialog.show_all()


if __name__ == "__main__":
    GLib.set_application_name("Meerkat Native Fixture")
    window = NativeFixture()
    window.show_all()
    Gtk.main()
