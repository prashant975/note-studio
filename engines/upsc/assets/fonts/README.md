# Embedded fonts

These files are embedded into every generated DCN DOCX (`word/fonts/`) so the
handwritten look survives on machines that never installed the faces.

| File | Face | Used for | Licence |
| --- | --- | --- | --- |
| `ComingSoon-regular.ttf` | Coming Soon (Open Window) | English body/headings | Apache 2.0 |
| `Kalam-Regular.ttf` | Kalam (Indian Type Foundry) | Hindi documents; Devanagari runs in English documents | OFL 1.1 (`OFL-Kalam.txt`) |
| `Kalam-Bold.ttf` | Kalam Bold | Bold runs in Hindi documents | OFL 1.1 (`OFL-Kalam.txt`) |

`ComingSoon-regular.ttf` is byte-identical to the copy carried by the PW
reference document (`word/fonts/ComingSoon-regular.ttf`), so template-mode and
programmatic-mode output embed the same file. The Kalam cuts are the Google
Fonts releases.
