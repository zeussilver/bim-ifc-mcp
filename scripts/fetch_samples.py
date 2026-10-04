"""Download the public sample models used for real-world testing into models/external/.

These files are third-party, so they are not committed; this script pins each one by
SHA-256 so tests always run against the exact bytes they were written for.

  - Duplex_A_20110907.ifc  IFC2X3, exported from Autodesk Revit Architecture 2011.
    "BSI (2020) 'Duplex Apartment Test Files', buildingSMART International", CC BY 4.0,
    https://github.com/buildingsmart-community/Community-Sample-Test-Files
  - AC20-FZK-Haus.ifc      IFC4, exported from ArchiCAD 20 by KIT IAI, unrestricted use,
    https://www.ifcwiki.org/index.php?title=KIT_IFC_Examples
"""

import hashlib
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "models" / "external"

SAMPLES = [
    (
        "Duplex_A_20110907.ifc",
        "https://media.githubusercontent.com/media/buildingsmart-community/Community-Sample-Test-Files/main/"
        "IFC%202.3.0.1%20(IFC%202x3)/Duplex%20Apartment/Duplex_A_20110907.ifc",
        "b347a2c8aa8fff6db896a4417a9c50c22ac0ccd7c5cfc22b99b8d29336c606ed",
    ),
    (
        "AC20-FZK-Haus.ifc",
        "https://www.ifcwiki.org/images/e/e3/AC20-FZK-Haus.ifc",
        "70cc8ff245fc0894201d96496c031005a5cbd7a96b22d8a1b87c5a883fb77994",
    ),
]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, url, sha256 in SAMPLES:
        target = OUT / name
        if target.is_file() and _sha256(target) == sha256:
            print(f"ok       {target}")
            continue
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = resp.read()
        if hashlib.sha256(data).hexdigest() != sha256:
            raise SystemExit(f"checksum mismatch for {name} from {url}; refusing to save it")
        target.write_bytes(data)
        print(f"fetched  {target} ({len(data):,} bytes)")


if __name__ == "__main__":
    fetch()
