import { useState, type ReactNode } from "react";

export function SettingsSection({
  title,
  summary,
  changed,
  initiallyOpen = false,
  children,
}: {
  title: string;
  summary: string;
  changed: boolean;
  initiallyOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <details
      className="settings-section"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span className="settings-section-title">{title}</span>
        {changed && <span className="settings-changed">Изменено</span>}
        <span className="settings-section-summary">{summary}</span>
      </summary>
      <div className="settings-section-body">{children}</div>
    </details>
  );
}

// Native validation cannot focus an input inside a collapsed details element.
// Reveal all ancestors first, including nested advanced settings, then report.
export function revealInvalidField(form: HTMLFormElement): boolean {
  const invalid = Array.from(
    form.querySelectorAll<
      HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
    >("input, select, textarea"),
  ).find((field) => field.willValidate && !field.validity.valid);
  if (!invalid) return false;
  let ancestor = invalid.parentElement;
  while (ancestor && ancestor !== form) {
    if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
    ancestor = ancestor.parentElement;
  }
  requestAnimationFrame(() => {
    invalid.focus({ preventScroll: true });
    invalid.scrollIntoView({ block: "center" });
    invalid.reportValidity();
  });
  return true;
}
