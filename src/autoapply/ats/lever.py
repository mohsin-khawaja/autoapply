"""Lever adapter (SPEC.md §5, workstream C).

Lever apply pages (``jobs.lever.co/<org>/<id>/apply``) are classic single-page
POST forms: named inputs (``name``, ``email``, ``phone``, ``org``,
``urls[LinkedIn]``, ...), a ``resume`` file input, and custom question cards
whose textareas/selects sit under ``.application-question`` blocks labelled by
``.application-label`` divs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlparse

from autoapply.ats import base
from autoapply.browser import flag_field

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

_HOSTS = ("jobs.lever.co", "jobs.eu.lever.co")

#: JS that scans the Lever application form and returns field descriptors.
_EXTRACT_JS = """
() => {
  const form = document.querySelector('#application-form, form.application-form, form');
  if (!form) return [];
  const out = [];
  const seen = new Set();
  const els = form.querySelectorAll('input, textarea, select');
  for (const el of els) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    const name = el.getAttribute('name') || null;
    if (name && seen.has(name)) continue;
    if (name) seen.add(name);
    let fieldType;
    if (el.tagName === 'TEXTAREA') fieldType = 'textarea';
    else if (el.tagName === 'SELECT') fieldType = 'select';
    else if (type === 'file') fieldType = 'file';
    else if (type === 'email') fieldType = 'email';
    else if (type === 'tel') fieldType = 'tel';
    else if (type === 'radio') fieldType = 'radio';
    else if (type === 'checkbox') fieldType = 'checkbox';
    else fieldType = 'text';
    // Label: nearest .application-question ancestor's .application-label,
    // else <label for>, aria-label, placeholder.
    let label = '';
    const q = el.closest('.application-question, li.application-question');
    if (q) {
      const l = q.querySelector('.application-label');
      if (l) label = l.textContent.trim();
    }
    if (!label && el.id) {
      const l = form.querySelector(`label[for="${el.id}"]`);
      if (l) label = l.textContent.trim();
    }
    if (!label)
      label = el.getAttribute('aria-label') || el.getAttribute('placeholder') || name || '';
    const options = el.tagName === 'SELECT'
      ? [...el.options].map(o => o.textContent.trim()).filter(t => t)
      : [];
    const attrs = {};
    for (const a of ['id', 'placeholder', 'data-qa']) {
      const v = el.getAttribute(a);
      if (v) attrs[a] = v;
    }
    out.push({
      name, fieldType, label: label.replace(/\\s*[✱*]\\s*$/, '').trim(),
      required: el.hasAttribute('required') || /[✱*]\\s*$/.test(label),
      options, attrs,
    });
  }
  return out;
}
"""


@base.register
class LeverAdapter(base.BaseAdapter):
    """Adapter for jobs.lever.co / jobs.eu.lever.co application forms."""

    kind = base.ATSKind.LEVER

    @classmethod
    def detect(cls, url: str) -> bool:
        try:
            parsed = urlparse(url)
        except ValueError:
            return False
        return parsed.scheme in ("http", "https") and parsed.hostname in _HOSTS

    def extract_form(self, page: Page) -> list[base.FormField]:
        raw = page.evaluate(_EXTRACT_JS)
        fields: list[base.FormField] = []
        for i, r in enumerate(raw):
            name = r["name"]
            if name:
                selector = f'form [name="{name}"]'
            elif r["attrs"].get("id"):
                selector = f'#{r["attrs"]["id"]}'
            else:
                selector = f"form :is(input,textarea,select) >> nth={i}"
            fields.append(
                base.FormField(
                    key=name or r["attrs"].get("id") or f"field_{i}",
                    field_type=r["fieldType"],
                    label=r["label"],
                    selector=selector,
                    name=name,
                    required=bool(r["required"]),
                    options=r["options"],
                    attrs=r["attrs"],
                )
            )
        return fields

    def fill(self, page: Page, plan: base.FillPlan) -> base.FillResult:
        filled = 0
        needs: list[str] = []
        try:
            for fp in plan.fields:
                f = fp.field
                if fp.needs_input or fp.source == "unmapped":
                    needs.append(f.label or f.key)
                    _flag_quiet(page, f.selector)
                    continue
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
                elif f.field_type == "radio":
                    page.check(f'{f.selector}[value="{value}"]')
                elif f.field_type == "checkbox":
                    page.check(f.selector)
                else:
                    page.fill(f.selector, value)
                filled += 1
        except Exception as exc:  # noqa: BLE001 - report, never crash the run
            return base.FillResult(status="failed", filled_count=filled, error=str(exc))
        status: base.FillStatus = "needs_input" if needs else "filled"
        return base.FillResult(status=status, filled_count=filled, needs_input_labels=needs)


def _flag_quiet(page: Page, selector: str) -> None:
    """Flag a field for manual input; a missing selector must not abort the fill."""
    try:
        flag_field(page, selector)
    except Exception:  # noqa: BLE001
        pass
