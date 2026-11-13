#!/usr/bin/env python3
"""Pack the extension folder into a .vsix without npm.

A VSIX is a zip with `extension.vsixmanifest`, `[Content_Types].xml`, and the
extension files under `extension/`.

    python3 vscode/build_vsix.py [output.vsix]
"""

import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
INCLUDE = ["package.json", "extension.js", "README.md", "src", "media"]

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension=".json" ContentType="application/json"/>
  <Default Extension=".js" ContentType="application/javascript"/>
  <Default Extension=".md" ContentType="text/markdown"/>
  <Default Extension=".svg" ContentType="image/svg+xml"/>
  <Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
"""


def manifest(pkg: dict) -> str:
    kinds = ",".join(pkg.get("extensionKind", ["workspace"]))
    return f"""<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011" xmlns:d="http://schemas.microsoft.com/developer/vsx-schema-design/2011">
  <Metadata>
    <Identity Language="en-US" Id="{escape(pkg['name'])}" Version="{escape(pkg['version'])}" Publisher="{escape(pkg['publisher'])}"/>
    <DisplayName>{escape(pkg.get('displayName', pkg['name']))}</DisplayName>
    <Description xml:space="preserve">{escape(pkg.get('description', ''))}</Description>
    <Tags></Tags>
    <Categories>{escape(','.join(pkg.get('categories', ['Other'])))}</Categories>
    <GalleryFlags>Public</GalleryFlags>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{escape(pkg['engines']['vscode'])}"/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionDependencies" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionPack" Value=""/>
      <Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="{escape(kinds)}"/>
      <Property Id="Microsoft.VisualStudio.Code.LocalizedLanguages" Value=""/>
      <Property Id="Microsoft.VisualStudio.Services.GitHubFlavoredMarkdown" Value="true"/>
    </Properties>
  </Metadata>
  <Installation>
    <InstallationTarget Id="Microsoft.VisualStudio.Code"/>
  </Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
    <Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def files() -> list[Path]:
    out = []
    for name in INCLUDE:
        p = HERE / name
        if p.is_dir():
            out += sorted(f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts)
        elif p.is_file():
            out.append(p)
    return out


def build(dest: Path) -> Path:
    pkg = json.loads((HERE / "package.json").read_text())
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", manifest(pkg))
        for f in files():
            z.write(f, "extension/" + f.relative_to(HERE).as_posix())
    return dest


if __name__ == "__main__":
    pkg = json.loads((HERE / "package.json").read_text())
    default = HERE / "dist" / f"{pkg['publisher']}.{pkg['name']}-{pkg['version']}.vsix"
    out = build(Path(sys.argv[1]) if len(sys.argv) > 1 else default)
    print(out)
