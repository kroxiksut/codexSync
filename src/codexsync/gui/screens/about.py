"""About: what the program is, what it refuses to do, and which build this is.

The only screen that asks the core nothing. Everything here is either a fact
about the build (`Controller.about`, which reads no configuration and opens no
state directory) or a sentence from the catalogue, so it draws instantly and
cannot show a stale reading.

It is not decoration. Two of its blocks answer questions the rest of the window
can only answer one screen at a time: *what is this program allowed to do to my
machine* — the four rules every mutation obeys, in the same words the
documentation uses — and *which build am I running*, which is the first thing a
bug report needs and the one thing a packaged executable makes hard to find,
since a frozen build carries its own Python and its own copy of the package.

The build facts are deliberately copyable in one click and deliberately
narrow: a version, how the program was started, this machine's kind, and where
the config file is. No project name, no chat, no path inside `.codex` — what
this screen shows may be pasted into a public issue without reading it twice.
"""
from __future__ import annotations

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QGridLayout, QLabel

from ..controller import BuildInfo
from ..widgets import button, card, label, row
from .base import Model, Screen

REPOSITORY = "https://github.com/kroxiksut/codexSync"
#: The documentation is per language, so the link follows the window's language
#: and falls back to English for a language the docs do not have yet.
DOCS = "https://github.com/kroxiksut/codexSync/blob/main/docs/{language}/README.md"
DOCUMENTED_LANGUAGES = ("en", "ru", "zh")
CHANGELOG = "https://github.com/kroxiksut/codexSync/blob/main/CHANGELOG.md"
ISSUES = "https://github.com/kroxiksut/codexSync/issues"
LICENCE = "https://github.com/kroxiksut/codexSync/blob/main/LICENSE"

#: The four rules in the order a run meets them: read, refuse, back up, confirm.
RULES = ("read", "write", "backup", "plan")
#: What codexSync deliberately leaves to the person. Each one is a real limit
#: with a screen behind it, not a disclaimer.
LIMITS = ("cloud", "registry", "unproven")


class AboutModel(Model):
    def __init__(self) -> None:
        #: Whether the build facts were just copied, so the line under the
        #: button can say so. Forgotten on any rebuild, which is correct: it
        #: describes a click, not a state.
        self.copied = False


class AboutScreen(Screen):
    page = "about"
    scrollable = True

    def build(self) -> None:
        info = self.host.controller.about()

        summary = label(self.t("about.heading", version=info.version), "cardSummary")
        what, inner = card(self.t("about.what.title"), summary)
        inner.addWidget(label(self.t("about.what.body"), wrap=True))
        inner.addWidget(label(self.t("about.what.not"), "muted", wrap=True))
        self.body.addWidget(what)

        # The build facts come before the prose: a person opening this screen
        # is usually answering "which version am I running", and a bug report
        # needs that line before it needs anything else.
        build, build_inner = card(self.t("about.build.title"))
        build_inner.addWidget(label(self.t("about.build.caption"), "muted", wrap=True))
        # A grid of labels rather than a table: six known rows on a page that
        # already scrolls, where a table brings its own scrollbar and cuts the
        # last fact off at whatever height it decided to take.
        facts = QGridLayout()
        facts.setHorizontalSpacing(18)
        facts.setVerticalSpacing(6)
        facts.setColumnStretch(1, 1)
        self.values: list[QLabel] = []
        for index, (name, _value) in enumerate(self._rows()):
            facts.addWidget(label(name, "muted"), index, 0)
            value = label("", wrap=True)
            self.values.append(value)
            facts.addWidget(value, index, 1)
        build_inner.addLayout(facts)
        self.copy_button = button(self.t("about.build.copy"))
        self.copy_button.clicked.connect(self.copy_facts)
        self.copied_line = label("", "muted")
        build_inner.addLayout(row(self.copy_button, self.copied_line))
        self.body.addWidget(build)

        rules, rules_inner = card(self.t("about.rules.title"))
        for rule in RULES:
            rules_inner.addWidget(label(self.t(f"about.rule.{rule}.title"), "cardTitle"))
            rules_inner.addWidget(label(self.t(f"about.rule.{rule}.body"), "muted", wrap=True))
        self.body.addWidget(rules)

        limits, limits_inner = card(self.t("about.limits.title"))
        limits_inner.addWidget(label(self.t("about.limits.caption"), "muted", wrap=True))
        for limit in LIMITS:
            limits_inner.addWidget(label(self.t(f"about.limit.{limit}"), wrap=True))
        self.body.addWidget(limits)

        licence, licence_inner = card(self.t("about.licence.title"))
        licence_inner.addWidget(label(self.t("about.licence.body"), wrap=True))
        licence_inner.addWidget(label(self.t("about.licence.extra"), "muted", wrap=True))
        licence_inner.addWidget(self._links(((self.t("about.licence.link"), LICENCE),)))
        self.body.addWidget(licence)

        links, links_inner = card(self.t("about.links.title"))
        links_inner.addWidget(self._links((
            (self.t("about.links.docs"), DOCS.format(language=self._documentation_language())),
            (self.t("about.links.project"), REPOSITORY),
            (self.t("about.links.changelog"), CHANGELOG),
            (self.t("about.links.issues"), ISSUES),
        )))
        links_inner.addWidget(label(self.t("about.links.caption"), "muted", wrap=True))
        self.body.addWidget(links)

    def render(self) -> None:
        for widget, (_name, value) in zip(self.values, self._rows()):
            widget.setText(value)
        self.copied_line.setText(self.t("about.build.copied") if self.model.copied else "")

    # --- the build facts ------------------------------------------------------

    def _rows(self, info: BuildInfo | None = None) -> list[tuple[str, str]]:
        """The facts as they are drawn and as they are copied: one pair per line."""
        info = info if info is not None else self.host.controller.about()
        started = self.t("about.build.frozen") if info.frozen else self.t("about.build.source")
        if info.config_exists:
            config = info.config_path
        elif info.config_path:
            config = self.t("about.build.config_missing", path=info.config_path)
        else:
            config = self.t("about.build.config_none")
        return [
            (self.t("about.build.version"), info.version),
            (self.t("about.build.started"), started),
            (self.t("about.build.executable"), info.executable),
            (self.t("about.build.python"), info.python),
            (self.t("about.build.system"), f"{info.system} ({info.architecture})"),
            (self.t("about.build.config"), config),
        ]

    def copy_facts(self) -> None:
        """Put the same rows on the clipboard, one ``fact: value`` per line."""
        text = "\n".join(f"{name}: {value}" for name, value in self._rows())
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:  # pragma: no branch - absent only on a headless host
            clipboard.setText(text)
        self.model.copied = True
        self.render()

    # --- links ----------------------------------------------------------------

    def _documentation_language(self) -> str:
        language = self.host.catalog.language
        return language if language in DOCUMENTED_LANGUAGES else "en"

    def _links(self, items: tuple[tuple[str, str], ...]) -> QLabel:
        """One line of links that open in the browser, not inside the window."""
        text = self.t("common.separator").join(
            f'<a href="{url}">{name}</a>' for name, url in items
        )
        widget = label(text, wrap=True)
        widget.setOpenExternalLinks(True)
        return widget
