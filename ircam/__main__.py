import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")

from .ui.app import main  # noqa: E402

sys.exit(main(sys.argv))
