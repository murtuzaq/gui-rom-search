"""ROM Finder - a three-panel tkinter app for locating games/ROMs from legitimate sources.

Panel 1: search box + console filters
Panel 2: matching sources, ranked by fewest "hoops" (clicks/redirects to the file)
Panel 3: details for the selected source, with live redirect check and open-in-browser
"""

import json
import threading
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from tkinter import messagebox, ttk

SITES_FILE = Path(__file__).with_name("sites.json")

CONSOLES = [
    "PC", "Android", "NES", "SNES", "N64", "Game Boy", "GBA",
    "Nintendo DS", "Sega Genesis", "PlayStation", "PS2", "PSP", "Arcade",
]


def load_sites():
    try:
        with SITES_FILE.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        messagebox.showerror("ROM Finder", f"Could not load {SITES_FILE.name}:\n{e}")
        return []


def build_search_url(site, query):
    template = site.get("search_url")
    if template and query:
        return template.replace("{q}", urllib.parse.quote_plus(query))
    return site["url"]


class _CountingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self):
        self.chain = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append((code, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def count_redirects(url, timeout=10):
    """Return (redirect_chain, final_url, status) for a GET to url."""
    handler = _CountingRedirectHandler()
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ROMFinder/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return handler.chain, resp.geturl(), resp.status
    except urllib.error.HTTPError as e:
        return handler.chain, e.geturl() or url, e.code


class RomFinderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ROM Finder")
        self.geometry("1150x620")
        self.minsize(900, 450)

        self.sites = load_sites()
        self.redirect_cache = {}  # url -> result string
        self.visible_sites = {}   # tree item id -> site dict

        self.query_var = tk.StringVar()
        self.console_vars = {c: tk.BooleanVar(value=False) for c in CONSOLES}
        self.status_var = tk.StringVar(value="Enter a game name and pick consoles, then Search.")

        self._build_ui()
        self.search()

    # ---------- layout ----------

    def _build_ui(self):
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Header.TLabel", font=("Segoe UI", 11, "bold"))

        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        panes.add(self._build_search_panel(panes), weight=1)
        panes.add(self._build_results_panel(panes), weight=2)
        panes.add(self._build_details_panel(panes), weight=2)

        ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W,
                  padding=(6, 2)).pack(fill=tk.X, side=tk.BOTTOM)

    def _build_search_panel(self, parent):
        frame = ttk.Frame(parent, padding=8)

        ttk.Label(frame, text="Search", style="Header.TLabel").pack(anchor=tk.W)
        entry = ttk.Entry(frame, textvariable=self.query_var)
        entry.pack(fill=tk.X, pady=(4, 4))
        entry.bind("<Return>", lambda e: self.search())
        entry.focus_set()
        ttk.Button(frame, text="Search", command=self.search).pack(fill=tk.X)

        ttk.Separator(frame).pack(fill=tk.X, pady=10)
        ttk.Label(frame, text="Console", style="Header.TLabel").pack(anchor=tk.W)

        boxes = ttk.Frame(frame)
        boxes.pack(fill=tk.BOTH, expand=True, pady=(4, 4))
        for name in CONSOLES:
            ttk.Checkbutton(boxes, text=name, variable=self.console_vars[name],
                            command=self.search).pack(anchor=tk.W)

        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X)
        ttk.Button(btns, text="All", command=lambda: self._set_all(True)).pack(
            side=tk.LEFT, expand=True, fill=tk.X)
        ttk.Button(btns, text="None", command=lambda: self._set_all(False)).pack(
            side=tk.LEFT, expand=True, fill=tk.X)
        return frame

    def _build_results_panel(self, parent):
        frame = ttk.Frame(parent, padding=8)
        ttk.Label(frame, text="Sources (fewest hoops first)", style="Header.TLabel").pack(anchor=tk.W)

        cols = ("site", "hoops", "rating", "redirects")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings", selectmode="browse")
        for col, text, width, anchor in (
            ("site", "Site", 180, tk.W),
            ("hoops", "Hoops", 60, tk.CENTER),
            ("rating", "Rating", 70, tk.CENTER),
            ("redirects", "Redirects", 80, tk.CENTER),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "site"))

        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=(4, 0))
        scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=(4, 0))

        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        return frame

    def _build_details_panel(self, parent):
        frame = ttk.Frame(parent, padding=8)
        ttk.Label(frame, text="Details", style="Header.TLabel").pack(anchor=tk.W)

        self.details = tk.Text(frame, wrap=tk.WORD, state=tk.DISABLED, relief=tk.FLAT,
                               font=("Segoe UI", 10), padx=6, pady=6)
        self.details.tag_configure("title", font=("Segoe UI", 14, "bold"))
        self.details.tag_configure("label", font=("Segoe UI", 10, "bold"))
        self.details.tag_configure("link", foreground="#0645ad", underline=True)
        self.details.pack(fill=tk.BOTH, expand=True, pady=(4, 6))

        btns = ttk.Frame(frame)
        btns.pack(fill=tk.X)
        self.open_btn = ttk.Button(btns, text="Open in Browser", command=self.open_selected,
                                   state=tk.DISABLED)
        self.open_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self.check_btn = ttk.Button(btns, text="Check Redirects", command=self.check_selected,
                                    state=tk.DISABLED)
        self.check_btn.pack(side=tk.LEFT, expand=True, fill=tk.X)
        return frame

    # ---------- behavior ----------

    def _set_all(self, value):
        for var in self.console_vars.values():
            var.set(value)
        self.search()

    def selected_consoles(self):
        return {c for c, v in self.console_vars.items() if v.get()}

    def search(self):
        chosen = self.selected_consoles()
        matches = [s for s in self.sites if not chosen or chosen & set(s["consoles"])]
        matches.sort(key=lambda s: (s["hoops"], -s["rating"], s["name"].lower()))

        self.tree.delete(*self.tree.get_children())
        self.visible_sites.clear()
        query = self.query_var.get().strip()
        for site in matches:
            url = build_search_url(site, query)
            item = self.tree.insert("", tk.END, values=(
                site["name"], site["hoops"], "★" * site["rating"],
                self.redirect_cache.get(url, "—"),
            ))
            self.visible_sites[item] = site

        filt = ", ".join(sorted(chosen)) if chosen else "all consoles"
        self.status_var.set(f"{len(matches)} source(s) for {filt}"
                            + (f' — query: "{query}"' if query else ""))
        self._render_details(None)

    def selected_site(self):
        sel = self.tree.selection()
        return (sel[0], self.visible_sites[sel[0]]) if sel else (None, None)

    def show_details(self):
        _, site = self.selected_site()
        self._render_details(site)

    def _render_details(self, site):
        t = self.details
        t.configure(state=tk.NORMAL)
        t.delete("1.0", tk.END)
        if site is None:
            t.insert(tk.END, "Select a source to see details.")
            self.open_btn.configure(state=tk.DISABLED)
            self.check_btn.configure(state=tk.DISABLED)
        else:
            query = self.query_var.get().strip()
            url = build_search_url(site, query)
            chosen = self.selected_consoles()
            t.insert(tk.END, site["name"] + "\n\n", "title")
            rows = [
                ("Searching for", query or "(no query: opens site home page)"),
                ("Matching consoles", ", ".join(c for c in site["consoles"]
                                                if not chosen or c in chosen)),
                ("All consoles", ", ".join(site["consoles"])),
                ("License", site["license"]),
                ("Hoops (est. clicks to file)", str(site["hoops"])),
                ("Rating", "★" * site["rating"] + "☆" * (5 - site["rating"])),
                ("Redirects (measured)", self.redirect_cache.get(url, "not checked yet")),
            ]
            for label, value in rows:
                t.insert(tk.END, f"{label}: ", "label")
                t.insert(tk.END, value + "\n")
            t.insert(tk.END, "\nLink: ", "label")
            t.insert(tk.END, url + "\n", "link")
            if query and not site.get("search_url"):
                t.insert(tk.END, "(This site has no search URL; you'll need to search on the page.)\n")
            t.insert(tk.END, "\n" + site["description"] + "\n")
            self.open_btn.configure(state=tk.NORMAL)
            self.check_btn.configure(state=tk.NORMAL)
        t.configure(state=tk.DISABLED)

    def open_selected(self):
        _, site = self.selected_site()
        if site:
            webbrowser.open(build_search_url(site, self.query_var.get().strip()))

    def check_selected(self):
        item, site = self.selected_site()
        if not site:
            return
        url = build_search_url(site, self.query_var.get().strip())
        self.check_btn.configure(state=tk.DISABLED)
        self.status_var.set(f"Checking redirects for {site['name']}…")

        def worker():
            try:
                chain, final, status = count_redirects(url)
                result = f"{len(chain)} (HTTP {status})"
                summary = f"{site['name']}: {len(chain)} redirect(s), final URL {final}"
            except Exception as e:  # network errors, timeouts, SSL issues
                result = "error"
                summary = f"{site['name']}: check failed ({e})"
            self.after(0, lambda: self._redirect_done(item, url, result, summary))

        threading.Thread(target=worker, daemon=True).start()

    def _redirect_done(self, item, url, result, summary):
        self.redirect_cache[url] = result
        if self.tree.exists(item):
            self.tree.set(item, "redirects", result)
        self.status_var.set(summary)
        self.show_details()


if __name__ == "__main__":
    RomFinderApp().mainloop()
