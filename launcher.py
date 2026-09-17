#!/usr/bin/env python3
import configparser
import os
import re
import shlex
import shutil
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango


class Application:
    """Datos mínimos de una aplicación encontrada en un archivo .desktop."""

    def __init__(self, name, command, icon, terminal, keywords=None, comment=""):
        self.name = name
        self.command = command
        self.icon = icon
        self.terminal = terminal
        self.keywords = keywords if keywords is not None else []
        self.comment = comment


def localized_value(entry, key, lang):
    """Obtiene un valor localizado con fallback al valor base."""
    if lang and lang not in {"C", "POSIX"}:
        language_keys = ["{}[{}]".format(key, lang)]
        language = lang.split("_")[0]
        if language != lang:
            language_keys.append("{}[{}]".format(key, language))
        keys = language_keys + [key]
    else:
        keys = [key]

    for localized_key in keys:
        value = entry.get(localized_key, "").strip()
        if value:
            return value
    return ""


def desktop_directories():
    """Devuelve los directorios de aplicaciones, priorizando los del usuario."""
    data_home = os.environ.get(
        "XDG_DATA_HOME", os.path.expanduser("~/.local/share")
    )
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    directories = [os.path.join(data_home, "applications")]
    directories.extend(
        os.path.join(directory, "applications")
        for directory in data_dirs.split(os.pathsep)
        if directory
    )
    directories.extend(
        [
            "/var/lib/snapd/desktop/applications",
            os.path.join(data_home, "flatpak/exports/share/applications"),
            "/var/lib/flatpak/exports/share/applications",
        ]
    )

    return list(dict.fromkeys(directories))


def read_applications():
    """Lee los archivos .desktop disponibles y descarta los no visibles."""
    applications = {}
    hidden = set()
    lang = (
        os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG")
        or ""
    ).split(".")[0]

    for directory in desktop_directories():
        if not os.path.isdir(directory):
            continue

        try:
            filenames = sorted(os.listdir(directory))
        except OSError:
            continue

        for filename in filenames:
            if not filename.endswith(".desktop"):
                continue

            path = os.path.join(directory, filename)
            parser = configparser.ConfigParser(
                interpolation=None,
                strict=False,
                delimiters=("=",),
                comment_prefixes=("#",),
            )
            parser.optionxform = str

            try:
                with open(path, encoding="utf-8") as desktop_file:
                    parser.read_file(desktop_file)
                entry = parser["Desktop Entry"]
            except (OSError, UnicodeError, configparser.Error, KeyError):
                continue

            if entry.get("Type") != "Application":
                continue
            if entry.get("Hidden", "false").lower() == "true":
                hidden.add(filename)
                applications.pop(filename, None)
                continue
            if filename in hidden:
                continue
            if entry.get("NoDisplay", "false").lower() == "true":
                continue

            name = localized_value(entry, "Name", lang)
            command = entry.get("Exec", "").strip()
            if not name or not command:
                continue

            icon = entry.get("Icon", "").strip()
            comment = localized_value(entry, "Comment", lang)
            keywords = [
                keyword.strip().casefold()
                for keyword in localized_value(entry, "Keywords", lang).split(";")
                if keyword.strip()
            ]
            command_parts = command_arguments(command, icon)
            if not command_parts or not executable_exists(command_parts[0]):
                continue

            # Los archivos del usuario deben reemplazar la misma entrada global.
            applications[filename] = Application(
                name=name,
                command=command,
                icon=icon,
                terminal=entry.get("Terminal", "false").lower() == "true",
                keywords=keywords,
                comment=comment,
            )

    return sorted(applications.values(), key=lambda app: app.name.casefold())


def command_arguments(exec_line, icon=""):
    """Convierte Exec en argumentos y elimina los códigos de campo del estándar."""
    try:
        arguments = shlex.split(exec_line)
    except ValueError:
        return []

    field_code = re.compile(r"%(.)")
    cleaned_arguments = []
    for argument in arguments:
        if argument == "%i":
            cleaned_arguments.extend(["--icon", icon] if icon else [])
            continue
        if argument in {"%u", "%f", "%U", "%F", "%c", "%k"}:
            continue

        def replace_field_code(match):
            code = match.group(1)
            if code == "%":
                return "%"
            if code in {"u", "f", "U", "F", "i", "c", "k"}:
                return ""
            return match.group(0)

        cleaned_arguments.append(field_code.sub(replace_field_code, argument))
    return cleaned_arguments


def executable_exists(command):
    """Comprueba ejecutables por nombre y por ruta absoluta."""
    if os.path.isabs(command):
        return os.path.isfile(command) and os.access(command, os.X_OK)
    return shutil.which(command) is not None


class LauncherWindow(Gtk.Window):
    ICON_SIZE = 32

    def __init__(self, applications):
        super().__init__()
        self.applications = applications
        self.filtered_applications = []
        self.set_name("launcher-window")
        self.set_title("Dracula Launcher")
        self.set_wmclass("DraculaLauncher", "DraculaLauncher")
        self.set_decorated(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_default_size(480, 400)
        self.set_keep_above(True)
        self.set_resizable(False)
        self.connect("destroy", Gtk.main_quit)

        self.build_ui()
        self.load_css()
        self.filter_applications("")

    def build_ui(self):
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        container.set_name("launcher-container")
        container.set_border_width(14)
        self.add(container)

        self.search_entry = Gtk.Entry()
        self.search_entry.set_name("search-entry")
        self.search_entry.set_placeholder_text("Buscar aplicaciones...")
        self.search_entry.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "system-search-symbolic")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect("changed", self.on_search_changed)
        self.search_entry.connect("key-press-event", self.on_search_key_press)
        container.pack_start(self.search_entry, False, False, 0)

        self.results = Gtk.ListBox()
        self.results.set_name("results-list")
        self.results.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.results.set_activate_on_single_click(True)
        self.results.connect("row-activated", self.on_row_activated)

        self.results_scroll = Gtk.ScrolledWindow()
        self.results_scroll.set_name("results-scroll")
        self.results_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.results_scroll.set_shadow_type(Gtk.ShadowType.NONE)
        self.results_scroll.set_hexpand(True)
        self.results_scroll.set_vexpand(True)
        self.results_scroll.add(self.results)
        container.pack_start(self.results_scroll, True, True, 0)

    def load_css(self):
        provider = Gtk.CssProvider()
        css_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "style.css")
        try:
            provider.load_from_path(css_path)
        except GLib.Error as error:
            print("No se pudo cargar style.css: {}".format(error), file=sys.stderr)
            return

        try:
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        except AttributeError:
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def on_search_changed(self, entry):
        self.filter_applications(entry.get_text())

    def filter_applications(self, query):
        query = query.casefold().strip()
        self.filtered_applications = [
            app
            for app in self.applications
            if query in app.name.casefold()
            or query in app.comment.casefold()
            or any(query in keyword for keyword in app.keywords)
        ]

        for child in self.results.get_children():
            self.results.remove(child)

        for app in self.filtered_applications:
            row = self.create_row(app)
            self.results.add(row)

        self.results.show_all()
        first_row = self.results.get_row_at_index(0)
        if first_row is not None:
            self.results.select_row(first_row)
            self.scroll_to_row(first_row)

    def create_row(self, app):
        row = Gtk.ListBoxRow()
        row.application = app

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_border_width(7)
        image = self.create_icon(app.icon)
        content.pack_start(image, False, False, 0)

        label = Gtk.Label(label=app.name, xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        content.pack_start(label, True, True, 0)
        row.add(content)
        return row

    @staticmethod
    def create_icon(icon_name):
        if icon_name and os.path.isabs(icon_name):
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                    icon_name, LauncherWindow.ICON_SIZE, LauncherWindow.ICON_SIZE
                )
                return Gtk.Image.new_from_pixbuf(pixbuf)
            except (GLib.Error, OSError):
                pass

        if icon_name:
            try:
                theme = Gtk.IconTheme.get_default()
                pixbuf = theme.load_icon(
                    icon_name,
                    LauncherWindow.ICON_SIZE,
                    Gtk.IconLookupFlags.FORCE_SIZE,
                )
                return Gtk.Image.new_from_pixbuf(pixbuf)
            except GLib.Error:
                pass

        try:
            theme = Gtk.IconTheme.get_default()
            pixbuf = theme.load_icon(
                "application-x-executable",
                LauncherWindow.ICON_SIZE,
                Gtk.IconLookupFlags.FORCE_SIZE,
            )
            return Gtk.Image.new_from_pixbuf(pixbuf)
        except GLib.Error:
            image = Gtk.Image.new_from_icon_name(
                "application-x-executable", Gtk.IconSize.MENU
            )
            image.set_pixel_size(LauncherWindow.ICON_SIZE)
            return image

    def on_search_key_press(self, _entry, event):
        key = event.keyval
        selected = self.results.get_selected_row()
        current_index = selected.get_index() if selected else -1

        if key in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            next_index = min(current_index + 1, len(self.filtered_applications) - 1)
            self.select_index(next_index)
            return True
        if key in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            previous_index = max(current_index - 1, 0)
            self.select_index(previous_index)
            return True
        if key in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.launch_selected()
            return True
        if key == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def select_index(self, index):
        if index < 0 or index >= len(self.filtered_applications):
            return
        row = self.results.get_row_at_index(index)
        self.results.select_row(row)
        self.scroll_to_row(row)

    def scroll_to_row(self, row):
        """Ajusta el scroll vertical para mantener visible la fila seleccionada."""
        if row is None:
            return

        # GTK actualiza las asignaciones después de procesar el evento de teclado.
        GLib.idle_add(self._ensure_row_visible, row, priority=GLib.PRIORITY_HIGH_IDLE)

    def _ensure_row_visible(self, row):
        if not row.get_mapped():
            return False

        adjustment = self.results_scroll.get_vadjustment()
        allocation = row.get_allocation()
        visible_top = adjustment.get_value()
        visible_bottom = visible_top + adjustment.get_page_size()
        row_top = allocation.y
        row_bottom = row_top + allocation.height

        if row_top < visible_top:
            adjustment.set_value(row_top)
        elif row_bottom > visible_bottom:
            adjustment.set_value(row_bottom - adjustment.get_page_size())
        return False

    def on_row_activated(self, _listbox, row):
        self.launch_application(row.application)

    def launch_selected(self):
        row = self.results.get_selected_row()
        if row is not None:
            self.launch_application(row.application)
        elif self.filtered_applications:
            self.launch_application(self.filtered_applications[0])

    def launch_application(self, application):
        arguments = command_arguments(application.command, application.icon)
        if not arguments:
            return

        if application.terminal:
            terminals = [os.environ.get("TERMINAL", ""), "x-terminal-emulator"]
            terminals.extend(["alacritty", "kitty", "wezterm", "gnome-terminal", "xterm"])
            terminal = next((candidate for candidate in terminals if candidate and shutil.which(candidate)), None)
            if terminal is None:
                print("No se encontró una terminal para ejecutar {}".format(application.name), file=sys.stderr)
                return
            arguments = [terminal, "-e", *arguments]

        try:
            subprocess.Popen(
                arguments,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            print("No se pudo ejecutar {}: {}".format(application.name, error), file=sys.stderr)
        finally:
            self.destroy()


def main():
    window = LauncherWindow(read_applications())
    window.show_all()
    window.present()
    window.search_entry.grab_focus()
    Gtk.main()


if __name__ == "__main__":
    main()