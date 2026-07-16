"""Greenhouse ATS adapter (workstream A, SPEC.md §9).

Handles both the classic ``boards.greenhouse.io`` embedded forms and the newer
React-based ``job-boards.greenhouse.io`` UI. Registers itself at import time via
:func:`autoapply.ats.base.register`.

SAFETY: :meth:`GreenhouseAdapter.fill` never clicks Submit. It fills mapped
fields, uploads the resume to file inputs, and red-outlines ``needs_input``
fields for the human.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from autoapply.ats import base
from autoapply.ats.base import ATSKind, FieldPlan, FillPlan, FillResult, FormField
from autoapply.browser import flag_field

if TYPE_CHECKING:  # pragma: no cover - hints only
    from playwright.sync_api import Locator, Page

_GREENHOUSE_HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io")

#: JS that walks the application form and returns raw field descriptors.
_EXTRACT_JS = """
() => {
  const form =
    document.querySelector('#application-form') ||
    document.querySelector('#application_form') ||
    document.querySelector('form[action*="greenhouse"]') ||
    document.querySelector('form');
  if (!form) return [];

  const results = [];
  const seenRadioGroups = new Set();

  const labelTextFor = (el) => {
    let text = '';
    if (el.id) {
      const lab = form.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lab) text = lab.textContent;
    }
    if (!text) {
      const wrap = el.closest('label');
      if (wrap) text = wrap.textContent;
    }
    if (!text) text = el.getAttribute('aria-label') || '';
    if (!text) {
      const labelledBy = el.getAttribute('aria-labelledby');
      if (labelledBy) {
        text = labelledBy
          .split(/\\s+/)
          .map((id) => (document.getElementById(id) || {}).textContent || '')
          .join(' ');
      }
    }
    if (!text) text = el.getAttribute('placeholder') || '';
    return text.replace(/\\s+/g, ' ').trim();
  };

  const isRequired = (el, label) =>
    el.required ||
    el.getAttribute('aria-required') === 'true' ||
    /[*✱]\\s*$/.test(label);

  const groupOf = (el) => {
    const fs = el.closest('fieldset');
    if (fs) {
      const leg = fs.querySelector('legend');
      if (leg) return leg.textContent.replace(/\\s+/g, ' ').trim();
    }
    const sec = el.closest('section[aria-label], [data-section-title]');
    if (sec)
      return (
        sec.getAttribute('aria-label') ||
        sec.getAttribute('data-section-title') ||
        null
      );
    return null;
  };

  const attrsOf = (el) => {
    const out = {};
    for (const a of ['id', 'data-testid', 'placeholder', 'aria-describedby', 'autocomplete'])
      if (el.getAttribute(a)) out[a] = el.getAttribute(a);
    for (const a of el.attributes) if (a.name.startsWith('aria-')) out[a.name] = a.value;
    return out;
  };

  const els = form.querySelectorAll('input, textarea, select');
  for (const el of els) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    // intl-tel-input internals (phone country search) are widget chrome, not questions
    if ((el.id || '').startsWith('iti-')) continue;
    if (el.disabled) continue;

    const label = labelTextFor(el);
    let fieldType;
    let options = [];
    let key = el.id || el.name || '';

    if (tag === 'select') {
      fieldType = el.multiple ? 'multiselect' : 'select';
      options = Array.from(el.options)
        .filter((o) => o.value !== '')
        .map((o) => o.textContent.replace(/\\s+/g, ' ').trim());
    } else if (tag === 'textarea') {
      fieldType = 'textarea';
    } else if (type === 'file') {
      fieldType = 'file';
    } else if (type === 'radio' || type === 'checkbox') {
      const name = el.name || el.id;
      if (type === 'radio') {
        if (seenRadioGroups.has(name)) continue;
        seenRadioGroups.add(name);
        fieldType = 'radio';
        options = Array.from(
          form.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`)
        ).map((r) => labelTextFor(r));
        // group label: fieldset legend or aria-labelledby of the group
        const fs = el.closest('fieldset');
        const legend = fs && fs.querySelector('legend');
        results.push({
          key: name,
          fieldType,
          label: legend ? legend.textContent.replace(/\\s+/g, ' ').trim() : label,
          selector: `input[type="radio"][name="${name}"]`,
          name,
          required: isRequired(el, label),
          options,
          autocomplete: el.getAttribute('autocomplete'),
          group: groupOf(el),
          attrs: attrsOf(el),
        });
        continue;
      }
      fieldType = 'checkbox';
    } else if (
      el.getAttribute('role') === 'combobox' ||
      (el.closest('[class*="select__"]') && tag === 'input')
    ) {
      fieldType = 'combobox';
      const listId = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
      const container = el.closest('[class*="select"], [data-options]');
      if (container && container.getAttribute('data-options')) {
        try { options = JSON.parse(container.getAttribute('data-options')); } catch (e) {}
      }
      if (!options.length && listId) {
        const list = document.getElementById(listId);
        if (list)
          options = Array.from(list.querySelectorAll('[role="option"], li')).map((o) =>
            o.textContent.replace(/\\s+/g, ' ').trim()
          );
      }
    } else if (type === 'email') {
      fieldType = 'email';
    } else if (type === 'tel') {
      fieldType = 'tel';
    } else if (type === 'date') {
      fieldType = 'date';
    } else if (type === '' || type === 'text' || type === 'search' || type === 'number') {
      fieldType = 'text';
    } else {
      fieldType = 'unknown';
    }

    const selector = el.id
      ? `#${CSS.escape(el.id)}`
      : el.name
        ? `${tag}[name="${el.name}"]`
        : null;
    if (!selector) continue;
    if (!key) key = selector;

    results.push({
      key,
      fieldType,
      label,
      selector,
      name: el.name || null,
      required: isRequired(el, label),
      options,
      autocomplete: el.getAttribute('autocomplete'),
      group: groupOf(el),
      attrs: attrsOf(el),
    });
  }
  return results;
}
"""


class GreenhouseAdapter(base.BaseAdapter):
    """Adapter for Greenhouse job boards (classic and React job-boards UI)."""

    kind = ATSKind.GREENHOUSE

    @classmethod
    def detect(cls, url: str) -> bool:
        """True for boards.greenhouse.io / job-boards.greenhouse.io / *.greenhouse.io."""
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return False
        return host in _GREENHOUSE_HOSTS or host.endswith(".greenhouse.io")

    def extract_form(self, page: Page) -> list[FormField]:
        """Discover fillable fields on the loaded Greenhouse application page."""
        raw: list[dict] = page.evaluate(_EXTRACT_JS)
        fields: list[FormField] = []
        seen: set[str] = set()
        for r in raw:
            key = r["key"]
            if key in seen:
                key = f"{key}:{len(fields)}"
            seen.add(key)
            fields.append(
                FormField(
                    key=key,
                    field_type=r["fieldType"],
                    label=r["label"],
                    selector=r["selector"],
                    name=r.get("name"),
                    required=bool(r.get("required")),
                    options=[o for o in r.get("options", []) if o],
                    autocomplete=r.get("autocomplete"),
                    group=r.get("group"),
                    attrs=r.get("attrs") or {},
                )
            )
        return fields

    def fill(self, page: Page, plan: FillPlan) -> FillResult:
        """Fill planned fields, upload resume, flag needs_input. Never submits."""
        filled = 0
        flagged: list[str] = []
        for fp in plan.fields:
            if fp.needs_input or fp.source == "unmapped":
                self._flag(page, fp)
                flagged.append(fp.field.label or fp.field.key)
                continue
            try:
                if self._fill_one(page, fp, plan):
                    filled += 1
            except Exception as exc:  # noqa: BLE001 - flag this field, keep filling the rest
                fp.needs_input = True
                fp.note = f"fill error: {type(exc).__name__}"
                self._flag(page, fp)
                flagged.append(fp.field.label or fp.field.key)

        required_unresolved = [
            fp.field.label or fp.field.key
            for fp in plan.fields
            if fp.field.required and (fp.needs_input or fp.source == "unmapped")
        ]
        status = "needs_input" if required_unresolved else "filled"
        return FillResult(
            status=status,
            filled_count=filled,
            needs_input_labels=flagged,
            notes=f"greenhouse: filled {filled}, flagged {len(flagged)}",
        )

    # ---- internals ----

    def _flag(self, page: Page, fp: FieldPlan) -> None:
        try:
            flag_field(page, fp.field.selector)
        except Exception:  # noqa: BLE001 - flagging is best-effort
            pass

    def _fill_one(self, page: Page, fp: FieldPlan, plan: FillPlan) -> bool:
        field = fp.field
        ftype = field.field_type

        if ftype == "file":
            path = fp.value if fp.value is not None else plan.resume_path
            if path is None:
                return False
            page.set_input_files(field.selector, str(path))
            return True

        if fp.value is None:
            return False
        value = fp.value

        if ftype in ("select", "multiselect"):
            labels = value if isinstance(value, list) else [str(value)]
            page.select_option(field.selector, label=labels)
            return True

        if ftype == "combobox":
            return self._fill_combobox(page, field.selector, str(value))

        if ftype == "radio":
            self._check_radio(page, field, str(value))
            return True

        if ftype == "checkbox":
            loc = page.locator(field.selector)
            if str(value).lower() in ("true", "yes", "1", "on"):
                loc.check()
            else:
                loc.uncheck()
            return True

        # text / email / tel / textarea / date / unknown
        page.fill(field.selector, str(value))
        return True

    def _fill_combobox(self, page: Page, selector: str, value: str) -> bool:
        """React-select style: click, type, pick the matching option."""
        loc = page.locator(selector)
        loc.click()
        loc.fill("")
        loc.type(value, delay=10)
        option = page.locator('[role="option"]', has_text=value).first
        try:
            option.wait_for(state="visible", timeout=3000)
        except Exception:  # noqa: BLE001 - option text rarely matches verbatim
            # Fixed lists filter by substring ("B.S." / "Bachelor of Science"
            # both miss "Bachelor's Degree"): retype the first word and take the
            # first option that contains it. Never pick an unfiltered option
            # blind — a wrong Degree is worse than a flagged one.
            head = value.split()[0].rstrip(",")
            loc.fill("")
            loc.type(head, delay=10)
            option = page.locator('[role="option"]', has_text=head).first
            option.wait_for(state="visible", timeout=3000)
        option.click()
        return True

    def _check_radio(self, page: Page, field: FormField, value: str) -> None:
        """Check the radio in the group whose label matches ``value``."""
        radios = page.locator(field.selector)
        count = radios.count()
        want = value.strip().lower()
        for i in range(count):
            radio = radios.nth(i)
            label = self._radio_label(page, radio)
            if label.strip().lower() == want:
                radio.check()
                return
        # fall back to matching input value attribute
        for i in range(count):
            radio = radios.nth(i)
            if (radio.get_attribute("value") or "").strip().lower() == want:
                radio.check()
                return
        raise ValueError(f"no radio option matching {value!r} for {field.key}")

    def _radio_label(self, page: Page, radio: Locator) -> str:
        return radio.evaluate(
            """el => {
                 if (el.id) {
                   const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                   if (lab) return lab.textContent;
                 }
                 const wrap = el.closest('label');
                 return wrap ? wrap.textContent : '';
               }"""
        )


base.register(GreenhouseAdapter)
