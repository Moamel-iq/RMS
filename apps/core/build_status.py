"""
Development build status.

Shown on the dashboard so the state of the build is visible in the product
rather than living in a chat log. Development visibility only — remove this
module, its card, and its test when Phase 0 exits.

Deliberately hand-maintained. Deriving it from anything would make it lie
sooner: a task is "complete" when its definition of done is met, which no
amount of introspection can establish.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

Label = str | Promise


class BuildState:
    COMPLETE = "COMPLETE"
    IN_PROGRESS = "IN_PROGRESS"
    NOT_STARTED = "NOT_STARTED"
    LOCKED = "LOCKED"


STATE_LABELS: dict[str, Label] = {
    BuildState.COMPLETE: _("مكتمل"),
    BuildState.IN_PROGRESS: _("قيد التنفيذ"),
    BuildState.NOT_STARTED: _("لم يبدأ"),
    BuildState.LOCKED: _("مقفل"),
}

STATE_CHIPS = {
    BuildState.COMPLETE: "chip--on",
    BuildState.IN_PROGRESS: "chip--brand",
    BuildState.NOT_STARTED: "chip--neutral",
    BuildState.LOCKED: "chip--off",
}


@dataclass(frozen=True)
class BuildItem:
    key: str
    label: Label
    state: str

    @property
    def state_label(self) -> Label:
        return STATE_LABELS[self.state]

    @property
    def chip_class(self) -> str:
        return STATE_CHIPS[self.state]

    @property
    def is_current(self) -> bool:
        return self.state == BuildState.IN_PROGRESS


PHASE_LABEL: Label = _("المرحلة ٠ — الأساسات")

BUILD_ITEMS: tuple[BuildItem, ...] = (
    BuildItem("0.1", _("التهيئة وفحص الجاهزية"), BuildState.COMPLETE),
    BuildItem("0.2", _("المستخدم المخصص وتسجيل الدخول"), BuildState.COMPLETE),
    BuildItem("0.3", _("المؤسسة والفروع والصلاحيات"), BuildState.COMPLETE),
    BuildItem("0.4", _("وحدات القياس"), BuildState.COMPLETE),
    BuildItem("0.5", _("أساس التدقيق"), BuildState.COMPLETE),
    BuildItem("0.6", _("نواة المحاسبة"), BuildState.NOT_STARTED),
    BuildItem("0.7", _("الصلاحيات والواجهات"), BuildState.IN_PROGRESS),
    BuildItem("0.8", _("إغلاق المرحلة ٠"), BuildState.NOT_STARTED),
    BuildItem("1", _("المرحلة ١ — المخزون"), BuildState.LOCKED),
)
