"""PB-021-A6 follow-up: invalid IDs and malformed external URI via real CLI."""
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit_probe as audit


def material_id(root, base):
    audit.write(root, "docs/specs/alpha.md", audit.text(audit.metadata(
        materials=[audit.material("proof.log", id=[], required=False)])))


def change_id(root, base):
    change = dict(id={}, state="draft", section="change-1", tasks=[], acceptance=None,
                  basis=dict(type="authorized_scope", who="engineer",
                             at="2026-10-01T00:00:00+03:00", detail="fixture", source="fixture"))
    audit.write(root, "docs/specs/alpha.md", audit.text(audit.metadata(changes=[change])))


def external_uri(root, base):
    audit.write(root, "docs/specs/alpha.md", audit.text(audit.metadata(materials=[
        audit.material("https://[", storage="external", access="fixture") ])))


if __name__ == "__main__":
    print(json.dumps([audit.probe("material-id-list", material_id),
                      audit.probe("change-id-object", change_id),
                      audit.probe("malformed-external-uri", external_uri)],
                     ensure_ascii=False, indent=2))
