"""Ashby (`jobs.ashbyhq.com`) adapter — SPEC.md §4, workstream B.

Ashby application pages are SPAs. Fields live in ``_fieldEntry``-style
container divs whose labels are plain elements (not always ``<label for>``),
forms can be split across progressively revealed sections/steps, and select
widgets are custom comboboxes (``role=combobox`` + ``aria-haspopup=listbox``)
that render their option list only after a click. Resume upload is a hidden
``input[type=file]``.

SAFETY: :meth:`AshbyAdapter.fill` never clicks Submit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from autoapply.ats import base
from autoapply.ats.base import ATSKind, FillPlan, FillResult, FormField
from autoapply.browser import flag_field

if TYPE_CHECKING:  # pragma: no cover - hints only
    from playwright.sync_api import Page

#: Attribute stamped onto elements during extraction so fill() can re-resolve them.
_MARK = "data-aa-field"

_EXTRACT_JS = """
() => {
  const results = [];
  const doneRadios = new Set();
  let idx = 0;

  const text = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');

  const groupOf = (el) => {
    let node = el.parentElement;
    while (node && node !== document.body) {
      if (node.matches('section, fieldset, [data-step], [class*="_section"], [class*="step"]')) {
        const h = node.querySelector(
          'legend, h1, h2, h3, h4, [class*="sectionTitle"], [class*="heading"]');
        const t = text(h);
        if (t) return t.split('\\n')[0].trim();
      }
      node = node.parentElement;
    }
    return null;
  };

  const containerOf = (el) =>
    el.closest('[class*="_fieldEntry"], [class*="fieldEntry"], .ashby-application-form-field-entry')
    || el.closest('fieldset, [role="radiogroup"], [role="group"]');

  const labelOf = (el) => {
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l && text(l)) return text(l);
    }
    const wrap = el.closest('label');
    if (wrap) {
      const t = text(wrap).split('\\n')[0].trim();
      if (t) return t;
    }
    const c = containerOf(el);
    if (c) {
      const l = c.querySelector('label, legend, [class*="label"], [class*="Label"]');
      if (l && text(l)) return text(l).split('\\n')[0].trim();
    }
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const t = labelledby.split(/\\s+/)
        .map(id => text(document.getElementById(id))).join(' ').trim();
      if (t) return t;
    }
    return el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || '';
  };

  const requiredOf = (el, label) => {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    const c = containerOf(el);
    if (c && c.querySelector('[class*="required"], [class*="Required"]')) return true;
    return /[*✱]\\s*$/.test(label);
  };

  const cleanLabel = (label) => label.replace(/\\s*[*✱]\\s*$/, '').trim();

  const attrsOf = (el) => {
    const out = {};
    for (const a of ['id', 'data-testid', 'placeholder', 'aria-haspopup', 'type']) {
      const v = el.getAttribute(a);
      if (v) out[a] = v;
    }
    return out;
  };

  const push = (el, fieldType, extra) => {
    const key = el.name || el.id || `field_${idx}`;
    el.setAttribute('data-aa-field', key);
    idx += 1;
    const rawLabel = labelOf(el);
    results.push(Object.assign({
      key,
      field_type: fieldType,
      label: cleanLabel(rawLabel),
      selector: `[data-aa-field="${key}"]`,
      name: el.name || null,
      required: requiredOf(el, rawLabel),
      options: [],
      autocomplete: el.getAttribute('autocomplete') || null,
      group: groupOf(el),
      attrs: attrsOf(el),
    }, extra || {}));
  };

  for (const el of document.querySelectorAll('input, textarea, select')) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (type === 'hidden' || type === 'submit' || type === 'button') continue;

    if (tag === 'select') {
      push(el, 'select', { options: Array.from(el.options).map(o => text(o)).filter(Boolean) });
      continue;
    }
    if (tag === 'textarea') { push(el, 'textarea'); continue; }
    if (type === 'radio') {
      const name = el.name || '';
      if (doneRadios.has(name)) continue;
      doneRadios.add(name);
      const radios = name
        ? Array.from(document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`))
        : [el];
      const opts = radios.map(r => {
        const wrap = r.closest('label');
        if (wrap) return text(wrap);
        if (r.id) {
          const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
          if (l) return text(l);
        }
        return r.value;
      }).filter(Boolean);
      const container = containerOf(el) || el;
      const key = name || el.id || `field_${idx}`;
      container.setAttribute('data-aa-field', key);
      idx += 1;
      const l = container.querySelector(
        'legend, [class*="label"], [class*="Label"], label:not(:has(input))');
      const rawLabel = l ? text(l).split('\\n')[0].trim() : (name || '');
      results.push({
        key,
        field_type: 'radio',
        label: cleanLabel(rawLabel),
        selector: `[data-aa-field="${key}"]`,
        name: name || null,
        required: radios.some(r => r.required) || container.getAttribute('aria-required') === 'true'
          || /[*✱]\\s*$/.test(rawLabel)
          || !!container.querySelector('[class*="required"], [class*="Required"]'),
        options: opts,
        autocomplete: null,
        group: groupOf(el),
        attrs: attrsOf(container),
      });
      continue;
    }
    if (type === 'checkbox') { push(el, 'checkbox'); continue; }
    if (type === 'file') { push(el, 'file'); continue; }
    if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox') {
      push(el, 'combobox');
      continue;
    }
    if (type === 'email') { push(el, 'email'); continue; }
    if (type === 'tel') { push(el, 'tel'); continue; }
    if (type === 'date') { push(el, 'date'); continue; }
    if (type === 'text' || type === 'search' || type === 'url' || type === 'number') {
      push(el, 'text');
      continue;
    }
    push(el, 'unknown');
  }
  return results;
}
"""


@base.register
class AshbyAdapter(base.BaseAdapter):
    """Adapter for Ashby-hosted application forms."""

    kind = ATSKind.ASHBY

    @classmethod
    def detect(cls, url: str) -> bool:
        try:
            host = (urlparse(url).hostname or "").lower()
        except ValueError:
            return False
        return host == "jobs.ashbyhq.com"

    def extract_form(self, page: Page) -> list[FormField]:
        raw: list[dict[str, Any]] = page.evaluate(_EXTRACT_JS)
        fields = [FormField(**item) for item in raw]
        for f in fields:
            if f.field_type == "combobox" and not f.options:
                f.options = self._peek_combobox_options(page, f.selector)
        return fields

    def fill(self, page: Page, plan: FillPlan) -> FillResult:
        filled = 0
        needs_input: list[str] = []
        try:
            for fp in plan.fields:
                f = fp.field
                if fp.needs_input or fp.source == "unmapped":
                    self._flag(page, f.selector)
                    needs_input.append(f.label or f.key)
                    continue
                if f.field_type == "file":
                    path = fp.value if fp.value is not None else plan.resume_path
                    if path is None:
                        if f.required:
                            self._flag(page, f.selector)
                            needs_input.append(f.label or f.key)
                        continue
                    page.set_input_files(f.selector, str(path))
                    filled += 1
                    continue
                if fp.value is None:
                    if f.required:
                        self._flag(page, f.selector)
                        needs_input.append(f.label or f.key)
                    continue
                value = fp.value
                if self._fill_one(page, f, value):
                    filled += 1
                else:
                    self._flag(page, f.selector)
                    needs_input.append(f.label or f.key)
        except Exception as exc:  # noqa: BLE001 - report, never crash the run
            return FillResult(
                status="failed",
                filled_count=filled,
                needs_input_labels=needs_input,
                error=f"{type(exc).__name__}: {exc}",
            )
        if needs_input:
            return FillResult(
                status="needs_input", filled_count=filled, needs_input_labels=needs_input
            )
        return FillResult(status="filled", filled_count=filled)

    # ---- internals ----

    @staticmethod
    def _flag(page: Page, selector: str) -> None:
        try:
            flag_field(page, selector)
        except Exception:  # noqa: BLE001 - flagging is best-effort
            pass

    def _fill_one(self, page: Page, f: FormField, value: str | list[str] | Any) -> bool:
        """Fill a single field. Returns False if the value could not be applied."""
        if isinstance(value, list):
            value_str = value[0] if value else ""
        else:
            value_str = str(value)
        ft = f.field_type
        if ft in ("text", "email", "tel", "textarea", "date", "unknown"):
            page.fill(f.selector, value_str)
            return True
        if ft == "select":
            page.select_option(f.selector, label=value_str)
            return True
        if ft == "checkbox":
            truthy = value_str.strip().lower() in ("yes", "true", "1", "on", "checked")
            page.set_checked(f.selector, truthy)
            return True
        if ft == "radio":
            return self._pick_radio(page, f, value_str)
        if ft == "combobox":
            return self._pick_combobox(page, f.selector, value_str)
        return False

    @staticmethod
    def _pick_radio(page: Page, f: FormField, value: str) -> bool:
        group = page.locator(f.selector)
        radios = group.locator('input[type="radio"]')
        want = value.strip().lower()
        for i in range(radios.count()):
            radio = radios.nth(i)
            label = ""
            wrap = radio.locator("xpath=ancestor::label[1]")
            if wrap.count():
                label = wrap.inner_text().strip()
            if not label:
                label = radio.get_attribute("value") or ""
            if label.strip().lower() == want:
                radio.check()
                return True
        return False

    def _pick_combobox(self, page: Page, selector: str, value: str) -> bool:
        """Click the combobox, type the value, pick the matching rendered option."""
        box = page.locator(selector)
        box.click()
        box.fill("")
        box.type(value, delay=10)
        option = self._match_option(page, value)
        if option is None:
            page.keyboard.press("Escape")
            return False
        option.click()
        return True

    def _peek_combobox_options(self, page: Page, selector: str) -> list[str]:
        """Open a combobox to read its rendered options, then close it."""
        try:
            page.locator(selector).click()
            page.wait_for_selector('[role="option"]', timeout=1500)
            opts = [
                t.strip()
                for t in page.locator('[role="option"]').all_inner_texts()
                if t.strip()
            ]
            page.keyboard.press("Escape")
            return opts
        except Exception:  # noqa: BLE001 - options are advisory
            try:
                page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                pass
            return []

    @staticmethod
    def _match_option(page: Page, value: str):  # noqa: ANN205 - Locator, deferred import
        try:
            page.wait_for_selector('[role="option"]', timeout=3000)
        except Exception:  # noqa: BLE001
            return None
        options = page.locator('[role="option"]')
        want = value.strip().lower()
        exact = None
        prefix = None
        for i in range(options.count()):
            text = options.nth(i).inner_text().strip()
            low = text.lower()
            if low == want and exact is None:
                exact = options.nth(i)
            elif low.startswith(want) and prefix is None:
                prefix = options.nth(i)
        return exact or prefix
