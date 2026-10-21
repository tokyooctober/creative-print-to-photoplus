# creative-print-to-photoplus

Convert **Epson Creative Print** (Android) "Stored Data" saves into **Epson Photo+** (Windows) `.etdx` project files, so a layout made on the phone can be opened and finished on the PC.

> Unofficial hobby project. Not affiliated with or endorsed by Seiko Epson Corporation. "Epson", "Creative Print" and "Photo+" belong to their owners. Based on reverse engineering of the file formats of Creative Print 7.11.0 and Photo+ 4.0.4.0.

## What gets converted

| Android item | Photo+ result |
|---|---|
| Frame photos | Photo in the same template frame, same position and zoom |
| Cut-outs (photo with shape mask) | Free photo with a **crop shape**, original full-resolution picture; the shape can be changed again in Photo+ (crop dialog) |
| Stamps | Clip art |
| Text | Text frame (font size, colour, spacing, box size) |
| Backgrounds | Background pattern |

Cut-out masks are mapped between the two apps' 10 shapes (square, rounded square, circle, heart, star, flower, burst, bubble, diamond, clover). A cut-out falls back to a flattened PNG clip art if its mask is unknown, it has extra rotation/scale/background removal, there are more than 10 on a page (Photo+ limit), or the scale is out of Photo+'s range.

Not converted: outlines on text/stamps, photo crop settings on frame photos, effects.

## Requirements

- Windows with Epson Photo+ installed (used for layout templates and for exact image/text measuring through WPF)
- Python 3.9+ (standard library only)
- Windows PowerShell 5.1 (for `wpf_image_info.ps1`, `wpf_text_size.ps1`; use `--no-wpf` to measure images in Python instead, text then falls back to an estimate)
- The Creative Print APK (version 7.11.0 tested), only needed for templates and the bundled font. Download it yourself; it is not included here.

## Usage

Copy the save files (the `CCE-YYYYMMDD-HHMMSS` files, no extension) from the phone's `EpsonCollage` folder, then:

```
python epson_port.py info    CCE-20261020-173204
python epson_port.py extract CCE-20261020-173204 out_dir
python epson_port.py to-etdx . -o converted            # every save in the folder
python epson_port.py to-etdx CCE-20261020-173204 --apk creative.apk
```

Fonts used by text are written to `converted\fonts`; install them in Windows so Photo+ shows the same lettering.

## Notes on the formats

- Android save: `"ECC3"` + 8-byte big-endian header length + `Header.json` (`FileNameList`/`FileSizeList`) + the files concatenated (`P0000_info.json` layout, `P0000.thum`, photos, stamps, backgrounds, `CI_<id>_IF/_JF/_TF/_AF` custom images).
- Photo+ `.etdx`: a ZIP with `page.json`, `projectInfo.json`, `MasterTemplate/_info.json` and `<pageId>/_info.json` (the template including `paperSizeList`, which Photo+ requires), photos in `<pageId>/<dirId>/`, stamps and backgrounds in `<pageId>/`.
- Photo positions: Android uses an offset from the frame centre and a scale relative to "fill the frame"; Photo+ uses the position relative to the paper centre (paper at 360 units/inch) and a scale relative to the image size.
- Shape masks: Photo+ has no mask object; a photo's `crop` is `{"type": 1..10, "rect": [x, y, w, h]}` in original pixels. Android's `cropEdit.frameRate`/`center` give the same window (centre = image centre minus `center`).
