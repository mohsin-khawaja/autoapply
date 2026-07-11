"""Generic fallback adapter (SPEC.md §5, workstream C).

Best-effort scan of visible ``<input>``/``<textarea>``/``<select>`` elements
inside ``<form>`` elements on any http(s) page. Registered LAST so every more
specific adapter wins detection. Must degrade gracefully: when no form is
found, or required fields cannot be confidently filled, it returns
``needs_input`` — it never guesses and never fails hard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from autoapply.ats import base
from autoapply.browser import flag_field

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

#: JS that scans visible form fields, skipping hidden/search/nav noise.
_EXTRACT_JS = """
() => {
  const out = [];
  const forms = [...document.querySelectorAll('form')];
  const skipTypes = ['hidden', 'submit', 'button', 'image', 'reset', 'search'];
  let idx = 0;
  for (const form of forms) {
    // Skip obvious search/nav forms.
    const role = (form.getAttribute('role') || '').toLowerCase();
    if (role === 'search') continue;
    if (form.closest('nav, header[role="banner"]')) continue;
    for (const el of form.querySelectorAll('input, textarea, select')) {
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (skipTypes.includes(type)) continue;
      const style = window.getComputedStyle(el);
      const isFile = type === 'file';
      if (!isFile && (style.display === 'none' || style.visibility === 'hidden')) continue;
      if (!isFile && el.offsetParent === null && style.position !== 'fixed') continue;
      const name = el.getAttribute('name') || null;
      let fieldType;
      if (el.tagName === 'TEXTAREA') fieldType = 'textarea';
      else if (el.tagName === 'SELECT') fieldType = 'select';
      else if (isFile) fieldType = 'file';
      else if (type === 'email') fieldType = 'email';
      else if (type === 'tel') fieldType = 'tel';
      else if (type === 'radio') fieldType = 'radio';
      else if (type === 'checkbox') fieldType = 'checkbox';
      else if (type === 'date') fieldType = 'date';
      else if (['text', 'url', 'number', ''].includes(type)) fieldType = 'text';
      else fieldType = 'unknown';
      let label = '';
      if (el.id) {
        const l = document.querySelector(`label[for="${el.id}"]`);
        if (l) label = l.textContent.trim();
      }
      if (!label) {
        const wrap = el.closest('label');
        if (wrap) label = wrap.textContent.trim();
      }
      if (!label)
        label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || name || '';
      const options = el.tagName === 'SELECT'
        ? [...el.options].map(o => o.textContent.trim()).filter(t => t)
        : [];
      const attrs = {};
      for (const a of ['id', 'placeholder', 'autocomplete', 'data-testid']) {
        const v = el.getAttribute(a);
        if (v) attrs[a] = v;
      }
      el.setAttribute('data-autoapply-key', 'gk_' + idx);
      out.push({
        name, fieldType, label: label.trim(),
        required: el.hasAttribute('required') || el.getAttribute('aria-required') === 'true',
        autocomplete: el.getAttribute('autocomplete') || null,
        options, attrs, key: 'gk_' + idx,
      });
      idx += 1;
    }
  }
  return out;
}
"""


@base.register
class GenericAdapter(base.BaseAdapter):
    """Fallback adapter: any http(s) URL, best-effort form scan."""

    kind = base.ATSKind.GENERIC

    @classmethod
    def detect(cls, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        return parsed.scheme in ("http", "https")

    def extract_form(self, page: Page) -> list[base.FormField]:
        try:
            raw = page.evaluate(_EXTRACT_JS)
        except Exception:  # noqa: BLE001 - degrade gracefully, never fail hard
            return []
        fields: list[base.FormField] = []
        for r in raw:
            fields.append(
                base.FormField(
                    key=r["key"],
                    field_type=r["fieldType"],
                    label=r["label"],
                    selector=f'[data-autoapply-key="{r["key"]}"]',
                    name=r["name"],
                    required=bool(r["required"]),
                    options=r["options"],
                    autocomplete=r["autocomplete"],
                    attrs=r["attrs"],
                )
            )
        return fields

    def fill(self, page: Page, plan: base.FillPlan) -> base.FillResult:
        if not plan.fields:
            return base.FillResult(
                status="needs_input",
                notes="no application form detected; manual entry required",
            )
        filled = 0
        needs: list[str] = []
        for fp in plan.fields:
            f = fp.field
            if fp.needs_input or fp.source == "unmapped" or fp.confidence < 0.5:
                needs.append(f.label or f.key)
                _flag_quiet(page, f.selector)
                continue
            try:
                if f.field_type == "file":
                    if plan.resume_path is not None:
                        page.set_input_files(f.selector, str(plan.resume_path))
                        filled += 1
                    else:
                        needs.append(f.label or f.key)
                        _flag_quiet(page, f.selector)
                    continue
                if fp.value is None:
                    continue
                value = fp.value if isinstance(fp.value, str) else str(fp.value)
                if f.field_type == "select":
                    page.select_option(f.selector, label=value)
                elif f.field_type == "checkbox":
                    page.check(f.selector)
                elif f.field_type in ("text", "email", "tel", "textarea", "date"):
                    page.fill(f.selector, value)
                else:
                    # Unknown widget: never guess.
                    needs.append(f.label or f.key)
                    _flag_quiet(page, f.selector)
                    continue
                filled += 1
            except Exception:  # noqa: BLE001 - per-field failure => flag, keep going
                needs.append(f.label or f.key)
                _flag_quiet(page, f.selector)
        status: base.FillStatus = "needs_input" if needs else "filled"
        return base.FillResult(status=status, filled_count=filled, needs_input_labels=needs)


def _flag_quiet(page: Page, selector: str) -> None:
    """Flag a field for manual input; a missing selector must not abort the fill."""
    try:
        flag_field(page, selector)
    except Exception:  # noqa: BLE001
        pass
