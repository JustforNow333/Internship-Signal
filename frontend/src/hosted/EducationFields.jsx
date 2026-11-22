import { useId } from "react";
import { CLASS_STANDING_OPTIONS, EDUCATION_LEVEL_OPTIONS } from "./constants.js";
import { withEducationLevel } from "./utils.js";

export const EDUCATION_HELP =
  "Not specified keeps every posting. Education rules apply only to internships, and postings with unknown requirements are always shown.";

// Shared by Settings and onboarding. `value` carries education_level and
// class_standing; `onChange` receives the whole updated preferences object.
export default function EducationFields({ value, onChange }) {
  const helpId = useId();
  const undergraduate = value.education_level === "undergraduate";
  return (
    <div className="education-fields">
      <p id={helpId} className="education-help">
        {EDUCATION_HELP}
      </p>
      <label className="field">
        <span>Education level</span>
        <select
          aria-describedby={helpId}
          value={value.education_level}
          onChange={(event) => onChange(withEducationLevel(value, event.target.value))}
        >
          {EDUCATION_LEVEL_OPTIONS.map((option) => (
            <option key={option.id} value={option.id}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      {undergraduate && (
        <label className="field">
          <span>Class standing</span>
          <select
            value={value.class_standing}
            onChange={(event) =>
              onChange({ ...value, class_standing: event.target.value })
            }
          >
            {CLASS_STANDING_OPTIONS.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      )}
    </div>
  );
}
