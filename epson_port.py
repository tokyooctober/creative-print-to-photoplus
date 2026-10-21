#!/usr/bin/env python3
"""Port Epson Creative Print (Android) "Stored Data" files to Epson Photo+ (PC) .etdx.

Android save file (format v3, see CollageTaskSaveDocument.saveUserData):

    "ECC3" | header length (8 bytes, big-endian) | Header.json | file[0] .. file[n-1]

    Header.json lists FileNameList/FileSizeList; files are concatenated in that order.
    Per page N: PNNNN.thum (thumbnail), PNNNN_info.json (layout), PNNNN_I<frame>_<uuid>
    (frame photos), plus stamp PNGs, background patterns and CI_<id>_* custom images.

Epson Photo+ .etdx (a ZIP, see EPPlus ProjectManager.LoadUserTemplateCore):

    page.json, projectInfo.json, MasterTemplate/_info.json (the layout template),
    <pageId>/_info.json (template + editedPaperSize), <pageId>/<dirId>/<photo>,
    and stamp / background image files directly in <pageId>/.

Both apps share the layout JSON schema, but differ in:
  * photo position: Android stores the offset from the frame centre and a scale relative
    to "fill the frame"; Photo+ stores the position relative to the paper centre and a
    scale relative to the image's 96-dpi size (WPF BitmapFrame.Width).
  * stamps / custom images: Android sizes them from a box of 1/4 of the paper's short
    side; Photo+ uses an explicit size * scale, and needs the image file in the page folder.
  * Android does not save paperSizeList, which Photo+ requires; it is taken from the
    template (from the Android APK, or the Photo+ install).

Usage:
    python epson_port.py info    <android-save-file>
    python epson_port.py extract <android-save-file> <out-dir>
    python epson_port.py to-etdx <android-save-file-or-folder> [-o out] [--apk app.apk]
"""

import argparse
import copy
import glob
import json
import math
import os
import secrets
import shutil
import string
import struct
import subprocess
import sys
import tempfile
import zipfile

MAGIC_V3 = b"ECC3"
MAGIC_V1V2 = b"ECC1"
PC_APP_VERSION = "4.0.4.0"
PC_INSTALL_DIR = r"C:\Program Files (x86)\Epson Software\PhotoPlus"
ID_CHARS = string.ascii_letters + string.digits
HERE = os.path.dirname(os.path.abspath(__file__))

DATA_MODES = {0: "Collage", 4: "Disc Label", 16: "Photo Book", 18: "ID Photo",
              20: "Create Plus", 22: "ID Card"}

# Photo+ z-order bands (TemplateDataManager): frame photos 100, items from 1000.
PC_PHOTO_FRAME_Z = 100
PC_ITEM_Z_BASE = 1000

# Android cut-out mask id (customImage.cropEdit.maskId) -> Photo+ crop shape type
# (TemplatePhotoCrop.type, CropTypePath_NN). Both apps have 10 shapes in different order:
# Android: square, rounded square, circle, heart, star, flower, burst, bubble, diamond, clover.
MASK_TO_CROP_TYPE = {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 7, "7": 6, "8": 10, "9": 8, "10": 9}
PC_MAX_FREE_PHOTOS = 10             # TemplateDataManager.MAX_ADDITIONAL_PHOTO_NUM
PC_CROP_EDGE_MARGIN = 0.016       # fraction of the picture kept free at its right/bottom edge
PC_PHOTO_SCALE_RANGE = (0.2, 8.0)   # PhotoEditView.PHOTO_SCALE_MIN / MAX

# Android draws stamps / custom images in a square of 1/4 of the paper's short side,
# enlarged by 1.1851852 (DrawObject.getMatrix / setBaseRect).
ANDROID_OBJECT_BOX = 0.25
ANDROID_OBJECT_ENLARGE = 1.1851852

# Physical paper sizes (portrait, mm) for Photo+/ESC/P-R media IDs. Converted to the
# 1/360-inch layout units both apps use; the paper centre is the photo origin in Photo+.
PAPER_MM = {
    "A2": (420, 594), "A3": (297, 420), "A4": (210, 297), "A5": (148, 210), "A6": (105, 148),
    "B5": (182, 257), "LT": (215.9, 279.4), "LG": (215.9, 355.6), "HL": (139.7, 215.9),
    "KG": (101.6, 152.4), "6G": (203.2, 254), "LB": (89, 127), "2L": (127, 178),
    "HG": (100, 148), "OH": (200, 148), "HV": (101.6, 180.6), "CA": (54, 86),
    "3A": (329, 483), "S1": (89, 89), "S2": (127, 127),
}


class FormatError(Exception):
    pass


# ---------------------------------------------------------------- Android container

def read_android_save(path):
    """Return (header dict, {file name: bytes}) for an Android ECC3 save file."""
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic == MAGIC_V1V2:
            raise FormatError("old v1/v2 save (Java-serialized header); open and re-save it "
                              "in a current version of the Android app first")
        if magic != MAGIC_V3:
            raise FormatError("not an Epson Creative Print save file (bad magic %r)" % magic)
        (header_len,) = struct.unpack(">q", f.read(8))
        header = json.loads(f.read(header_len).decode("utf-8"))
        names = header.get("FileNameList", [])
        sizes = header.get("FileSizeList", [])
        if len(names) != len(sizes):
            raise FormatError("header FileNameList/FileSizeList length mismatch")
        files = {}
        for name, size in zip(names, sizes):
            data = f.read(size)
            if len(data) != size:
                raise FormatError("truncated file data for %s" % name)
            files[name] = data
    return header, files


def looks_like_android_save(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) in (MAGIC_V3, MAGIC_V1V2)
    except OSError:
        return False


# ---------------------------------------------------------------- image geometry

def image_extension(data):
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return ".heic"
    return ""


def _exif_values(tiff):
    """Return (orientation, xres_dpi) from a TIFF/EXIF block."""
    try:
        endian = "<" if tiff[:2] == b"II" else ">"
        (ifd,) = struct.unpack(endian + "I", tiff[4:8])
        (count,) = struct.unpack(endian + "H", tiff[ifd:ifd + 2])
        orientation, xres, unit = 1, None, 2
        for i in range(count):
            entry = tiff[ifd + 2 + 12 * i: ifd + 14 + 12 * i]
            tag, typ, _n = struct.unpack(endian + "HHI", entry[:8])
            if tag == 0x0112:
                orientation = struct.unpack(endian + "H", entry[8:10])[0]
            elif tag == 0x0128:
                unit = struct.unpack(endian + "H", entry[8:10])[0]
            elif tag == 0x011A and typ == 5:
                (off,) = struct.unpack(endian + "I", entry[8:12])
                num, den = struct.unpack(endian + "II", tiff[off:off + 8])
                xres = num / den if den else None
        if xres and unit == 3:
            xres *= 2.54
        return orientation, xres
    except (struct.error, IndexError):
        return 1, None


def image_geometry_python(data):
    """(px w, px h, dip w, dip h, orientation) like WPF BitmapFrame, best effort."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        dpi = 96.0
        p = 8
        while p + 8 <= len(data):
            length, ctype = struct.unpack(">I4s", data[p:p + 8])
            if ctype == b"pHYs":
                ppu, _ppu_y, unit = struct.unpack(">IIB", data[p + 8:p + 17])
                if unit == 1 and ppu:
                    dpi = ppu * 0.0254
            if ctype in (b"IDAT", b"IEND"):
                break
            p += 12 + length
        return w, h, w * 96.0 / dpi, h * 96.0 / dpi, 1
    if data[:2] == b"\xff\xd8":
        p, w, h, jfif_dpi, exif = 2, 0, 0, None, (1, None)
        while p + 4 <= len(data):
            if data[p] != 0xFF:
                break
            marker = data[p + 1]
            (length,) = struct.unpack(">H", data[p + 2:p + 4])
            seg = data[p + 4:p + 2 + length]
            if marker == 0xE0 and seg[:5] == b"JFIF\x00":
                unit = seg[7]
                xd = struct.unpack(">H", seg[8:10])[0]
                if unit == 1 and xd:
                    jfif_dpi = float(xd)
                elif unit == 2 and xd:
                    jfif_dpi = xd * 2.54
            elif marker == 0xE1 and seg[:6] == b"Exif\x00\x00":
                exif = _exif_values(seg[6:])
            elif marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", seg[1:5])
                break
            p += 2 + length
        dpi = jfif_dpi or exif[1] or 96.0
        orientation = exif[0] if 1 <= exif[0] <= 8 else 1
        dw, dh = w * 96.0 / dpi, h * 96.0 / dpi
        if orientation in (5, 6, 7, 8):
            w, h, dw, dh = h, w, dh, dw
        return w, h, dw, dh, orientation
    raise FormatError("unsupported image format")


def image_geometry_wpf(paths):
    """Measure images exactly as Photo+ does, via WPF (Windows only). {path: tuple}."""
    script = os.path.join(HERE, "wpf_image_info.ps1")
    if os.name != "nt" or not os.path.exists(script) or not paths:
        return {}
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script] + paths,
            capture_output=True, text=True, timeout=300).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    result = {}
    for line in out.splitlines():
        try:
            info = json.loads(line)
        except ValueError:
            continue
        if "error" in info:
            continue
        w, h, dw, dh, o = info["pw"], info["ph"], info["w"], info["h"], info["orientation"]
        if o in (5, 6, 7, 8):
            w, h, dw, dh = h, w, dh, dw
        result[os.path.normcase(os.path.abspath(info["path"]))] = (w, h, dw, dh, o)
    return result


def measure_text(text, family, font_path, em):
    """(width, height) of text at em size, via WPF when available, else an estimate."""
    script = os.path.join(HERE, "wpf_text_size.ps1")
    if os.name == "nt" and os.path.exists(script):
        import base64
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script,
                 "-FontFile", font_path or "", "-Family", family or "", "-EmSize", repr(em),
                 "-TextBase64", base64.b64encode(text.encode("utf-8")).decode("ascii")],
                capture_output=True, text=True, timeout=60).stdout
            size = json.loads(out.strip().splitlines()[-1])
            if size["w"] > 0 and size["h"] > 0:
                return size["w"], size["h"]
        except (OSError, subprocess.SubprocessError, ValueError, IndexError, KeyError):
            pass
    lines = text.split("\n") or [""]
    width = max(sum(1.0 if ord(c) > 0x2E80 else 0.6 for c in line) for line in lines) * em
    return width, len(lines) * em * 1.35


# ---------------------------------------------------------------- templates and fonts

class Resources:
    """Look up original templates and fonts in the Android APK or the Photo+ install."""

    def __init__(self, apk_path=None, pc_dir=PC_INSTALL_DIR):
        self.apk = zipfile.ZipFile(apk_path) if apk_path else None
        self.pc_dir = pc_dir if pc_dir and os.path.isdir(pc_dir) else None

    def template(self, template_id):
        if not template_id:
            return None, None
        group = template_id.rsplit("_", 1)[0]
        if self.apk:
            for name in ("assets/contents/templates/%s/%s/_info.json" % (group, template_id),
                         "assets/collage/templates/%s/%s/_info.json" % (group, template_id)):
                try:
                    return json.loads(self.apk.read(name).decode("utf-8-sig")), "Android APK"
                except KeyError:
                    pass
        if self.pc_dir:
            path = os.path.join(self.pc_dir, "templates", group, template_id + ".etdx")
            if os.path.exists(path):
                with zipfile.ZipFile(path) as z:
                    return json.loads(z.read("_info.json").decode("utf-8-sig")), "Photo+ install"
        return None, None

    def font(self, file_name):
        if not self.apk:
            return None
        wanted = "assets/fonts/" + file_name
        for info in self.apk.infolist():
            name = info.filename
            if not info.flag_bits & 0x800:
                # Entry name stored as raw UTF-8 without the ZIP UTF-8 flag.
                try:
                    name = name.encode("cp437").decode("utf-8")
                except UnicodeError:
                    pass
            if name == wanted:
                return self.apk.read(info)
        return None


def font_family_name(data):
    """Family name from an OpenType/TrueType 'name' table (prefer English, then Japanese)."""
    try:
        (num_tables,) = struct.unpack(">H", data[4:6])
        for i in range(num_tables):
            tag, _cs, offset, _len = struct.unpack(">4sIII", data[12 + 16 * i: 28 + 16 * i])
            if tag != b"name":
                continue
            _fmt, count, str_off = struct.unpack(">HHH", data[offset:offset + 6])
            names = {}
            for j in range(count):
                rec = data[offset + 6 + 12 * j: offset + 18 + 12 * j]
                pid, eid, lang, nid, length, off = struct.unpack(">HHHHHH", rec)
                if nid in (1, 16) and pid == 3:
                    raw = data[offset + str_off + off: offset + str_off + off + length]
                    names[(nid, lang)] = raw.decode("utf-16-be", "replace")
            for key in ((16, 0x409), (1, 0x409), (16, 0x411), (1, 0x411)):
                if key in names:
                    return names[key]
            if names:
                return next(iter(names.values()))
    except (struct.error, IndexError):
        pass
    return None


# ---------------------------------------------------------------- conversion

def new_id(taken):
    while True:
        value = "".join(secrets.choice(ID_CHARS) for _ in range(10))
        if value not in taken:
            taken.add(value)
            return value


def paper_units(paper_id, edited, warnings):
    """Paper (width, height) in 1/360 inch, in the layout's orientation."""
    landscape = edited.get("orientation") == 1 or (
        edited.get("size") and edited["size"][0] > edited["size"][1])
    if paper_id in PAPER_MM:
        w, h = (round(v * 360 / 25.4) for v in PAPER_MM[paper_id])
    else:
        sw, sh = edited["size"]
        tx, ty = edited.get("topleft", [0, 0])
        w, h = sw + 2 * tx, sh + 2 * ty
        if landscape:
            w, h = h, w
        warnings.append("paper size %r not in table; photo positions are approximate" % paper_id)
    return (h, w) if landscape else (w, h)


def frame_by_index(edited, index):
    for frame in edited.get("imageFrames", []):
        if frame.get("index") == index:
            return frame
    return None


class PageConverter:
    def __init__(self, files, resources, taken_ids, temp_dir, warnings):
        self.files = files
        self.resources = resources
        self.taken_ids = taken_ids
        self.temp_dir = temp_dir
        self.warnings = warnings
        self.entries = []          # (relative path in page folder, bytes)
        self.fonts = {}            # file name -> bytes

    def add_resource(self, name):
        """Copy an embedded file into the page folder; return its name or None."""
        if name in self.files:
            if name not in (e[0] for e in self.entries):
                self.entries.append((name, self.files[name]))
            return name
        return None

    def geometry(self, name):
        path = os.path.join(self.temp_dir, name)
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(self.files[name])
        return path

    def convert(self, page_json, template, measured):
        edited = page_json["editedPaperSize"]
        paper_id = edited.get("paperSizeId")
        pw, ph = paper_units(paper_id, edited, self.warnings)
        box = ANDROID_OBJECT_BOX * min(pw, ph) * ANDROID_OBJECT_ENLARGE

        template_frames = {}
        if template:
            for size in template.get("paperSizeList", []):
                if size.get("paperSizeId") == paper_id and \
                        size.get("orientation", 0) == edited.get("orientation", 0):
                    template_frames = {f.get("index"): f for f in size.get("imageFrames", [])}

        # Android writes "angle": "fill" over the frame angle (save bug); restore it.
        for frame in edited.get("imageFrames", []):
            if not isinstance(frame.get("angle"), (int, float)):
                original = template_frames.get(frame.get("index"), {})
                frame["angle"] = original.get("angle", 0) if isinstance(original.get("angle"), (int, float)) else 0
                frame.setdefault("fitting", original.get("fitting", "fill"))
            frame.setdefault("fitting", "fill")
            frame["workSpaceNumber"] = 1

        photos, cliparts = [], []
        for photo in edited.get("photos", []):
            name = photo.get("imageName") or ""
            if photo.get("frameIndex", 0) > 0:
                converted = self.frame_photo(photo, edited, pw, ph, measured)
                if converted:
                    photos.append(converted)
            elif name.startswith("CI_"):
                converted = None
                if sum(1 for c in cliparts if "imagepath" in c) < PC_MAX_FREE_PHOTOS:
                    converted = self.cutout_photo(photo, box, measured)
                if not converted:
                    converted = self.custom_image(photo, box, measured)
                if converted:
                    cliparts.append(converted)
            elif name:
                self.warnings.append("unsupported free photo %r skipped" % name)

        for stamp in edited.get("cliparts", []):
            converted = self.stamp(stamp, box, measured)
            if converted:
                cliparts.append(converted)

        messages = [self.message(m) for m in edited.get("messages", [])]

        # Keep Android's relative order of stamps, custom images and text.
        items = sorted(cliparts + messages, key=lambda item: item.get("zindex", 0))
        for z, item in enumerate(items):
            item["zindex"] = PC_ITEM_Z_BASE + z
        edited["photos"] = photos + [i for i in items if "imagepath" in i]
        edited["cliparts"] = [i for i in items if "clipartName" in i]
        edited["messages"] = [i for i in items if "content" in i]
        self.background(edited)
        edited.pop("disclabel", None)
        edited["workData"] = {"maxWorkSpaceCount": 1}

    def frame_photo(self, photo, edited, pw, ph, measured):
        name = photo.get("imageName") or ""
        if not name:
            return None                     # empty frame
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        if base not in self.files:
            self.warnings.append("photo %r not found inside the save file" % name)
            return None
        frame = frame_by_index(edited, photo["frameIndex"])
        if frame is None:
            self.warnings.append("photo %r refers to missing frame %s" % (name, photo["frameIndex"]))
            return None
        data = self.files[base]
        ext = image_extension(data) or ".jpg"
        w, h, dw, dh, _o = measured[base]

        fw, fh = frame["size"]
        fill = max(fw / w, fh / h)                       # Android DrawPhoto.getBaseScale
        display_w = w * fill * float(photo.get("scale", 1) or 1)
        ox, oy = (photo.get("center") or [0, 0])[:2]
        cx = frame["topleft"][0] + fw / 2.0 + ox - pw / 2.0
        cy = frame["topleft"][1] + fh / 2.0 + oy - ph / 2.0

        dir_id = new_id(self.taken_ids)
        file_name = base + ext
        self.entries.append((dir_id + "/" + file_name, data))
        out = {
            "imagepath": dir_id + "\\" + file_name,
            "originalsize": [int(w), int(h)],
            "center": [round(cx, 4), round(cy, 4)],
            "angle": photo.get("angle", 0),
            "scale": display_w / dw,
            "crop": {},
            "effectInfo": {},
            "frameIndex": photo["frameIndex"],
            "workSpaceNumber": 1,
            "zindex": PC_PHOTO_FRAME_Z,
        }
        if photo.get("cropEdit") or photo.get("crop"):
            self.warnings.append("crop settings on photo %r are not converted" % name)
        return out

    def _object(self, file_name, source, box, measured, label):
        w, h, _dw, _dh, _o = measured[file_name]
        if source.get("strokeThickness"):
            self.warnings.append("outline on %s is not converted" % label)
        return {
            "type": "stamp",
            "clipartName": file_name,
            "center": source.get("center", [0, 0]),
            "size": [int(w), int(h)],
            "angle": source.get("angle", 0),
            "scale": box * float(source.get("scale", 1) or 1) / max(w, h),
            "maxScale": 1,
            "workSpaceNumber": 1,
            "zindex": source.get("zindex", 0),
        }

    def stamp(self, stamp, box, measured):
        name = stamp.get("clipartName", "")
        if not self.add_resource(name):
            self.warnings.append("stamp %r not found inside the save file; skipped" % name)
            return None
        return self._object(name, stamp, box, measured, "stamp " + name)

    def cutout_photo(self, photo, box, measured):
        """Android cut-out (photo + shape mask) as a Photo+ free photo with a crop shape.

        Keeps the shape changeable in Photo+ (crop dialog) and the full-resolution original.
        Android cropEdit: frameRate = crop window as a fraction of the image, centred on the
        image centre minus `center` (original pixels; may reach outside the image), the
        mask shape stretched over the window. Returns None to fall back to the baked PNG.
        """
        stem = photo["imageName"][:-3] if photo["imageName"].endswith("_IF") else photo["imageName"]
        source, meta = stem + "_IF", self.files.get(stem + "_JF")
        if source not in self.files or meta is None:
            return None
        try:
            edit = json.loads(meta.decode("utf-8-sig"))["customImage"]["cropEdit"]
            rate_x, rate_y = edit["frameRate"]
            off_x, off_y = edit.get("center", [0, 0])
        except (ValueError, KeyError, TypeError):
            return None
        crop_type = MASK_TO_CROP_TYPE.get(str(edit.get("maskId")))
        if crop_type is None or edit.get("backgroundRemoved") or edit.get("angle") or                 abs(float(edit.get("scale", 1) or 1) - 1) > 1e-6:
            return None                    # unknown mask / extra edits: keep the baked PNG
        data = self.files[source]
        if image_extension(data) != ".jpg" or source not in measured:
            return None
        w, h, dw, _dh, _o = measured[source]
        win_w, win_h = rate_x * w, rate_y * h
        cx, cy = w / 2.0 - off_x, h / 2.0 - off_y
        # Photo+ cannot crop outside the picture: clamp the window to the image.
        # Keep a small margin at the right/bottom: the Photo+ crop dialog rounds the rect in
        # reduced-decode pixels and a rect flush with those edges ends up 1 px outside the
        # picture, which makes its mask preview fail (shape buttons stop working).
        x0, x1 = max(0.0, cx - win_w / 2), min(w * (1 - PC_CROP_EDGE_MARGIN), cx + win_w / 2)
        y0, y1 = max(0.0, cy - win_h / 2), min(h * (1 - PC_CROP_EDGE_MARGIN), cy + win_h / 2)
        rect = [int(round(x0)), int(round(y0)), int(round(x1 - x0)), int(round(y1 - y0))]
        if rect[2] < 1 or rect[3] < 1:
            return None
        k = box * float(photo.get("scale", 1) or 1) / max(win_w, win_h)     # layout units per image pixel
        angle = math.radians(photo.get("angle", 0) or 0)
        shift_x, shift_y = ((x0 + x1) / 2 - cx) * k, ((y0 + y1) / 2 - cy) * k
        ox, oy = photo.get("center", [0, 0])[:2]
        center = [ox + shift_x * math.cos(angle) - shift_y * math.sin(angle),
                  oy + shift_x * math.sin(angle) + shift_y * math.cos(angle)]
        # Photo+ free photos are drawn rect-aspect, width = scale * picture width.
        scale = rect[2] * k / (dw if abs(dw - w) < 0.5 else w)
        if not PC_PHOTO_SCALE_RANGE[0] <= scale <= PC_PHOTO_SCALE_RANGE[1]:
            return None
        dir_id = new_id(self.taken_ids)
        file_name = source + ".jpg"
        self.entries.append((dir_id + "/" + file_name, data))
        return {
            "imagepath": dir_id + "\\" + file_name,
            "originalsize": [int(w), int(h)],
            "center": [round(center[0], 4), round(center[1], 4)],
            "angle": photo.get("angle", 0),
            "scale": scale,
            "crop": {"type": crop_type, "rect": rect},
            "effectInfo": {},
            "frameIndex": 0,
            "workSpaceNumber": 1,
            "zindex": photo.get("zindex", 0),
        }

    def custom_image(self, photo, box, measured):
        # CI_<id>_AF / _TF are the finished cut-out (crop + mask) as a transparent PNG.
        stem = photo["imageName"][:-3] if photo["imageName"].endswith("_IF") else photo["imageName"]
        for suffix in ("_AF", "_TF"):
            source = stem + suffix
            if source in self.files:
                png_name = source + ".png"
                self.files[png_name] = self.files[source]
                measured[png_name] = measured[source]
                self.add_resource(png_name)
                return self._object(png_name, photo, box, measured, "image " + stem)
        self.warnings.append("custom image %r has no rendered PNG; skipped" % stem)
        return None

    def message(self, message):
        content = message.get("content", "")
        src_font = message.get("fontInfo") or {}
        file_name = src_font.get("fontName", "")
        family, font_path = file_name, None
        if file_name.lower().endswith((".otf", ".ttf", ".ttc")):
            data = self.resources.font(file_name)
            family = (font_family_name(data) if data else None) or os.path.splitext(file_name)[0]
            if data:
                self.fonts[file_name] = data
                font_path = os.path.join(self.temp_dir, "font_" + str(len(self.fonts)) + os.path.splitext(file_name)[1])
                with open(font_path, "wb") as f:
                    f.write(data)
            else:
                self.warnings.append("font %r not found in the APK" % file_name)
        if message.get("strokeThickness"):
            self.warnings.append("outline on text %r is not converted" % content)

        # Android draws text at fontsize * scale (font size in points, 360 layout units
        # per inch) and stores the centre of the text. Photo+ text frames ("textframe",
        # as in its own templates) store the font size and an explicit box; without a box
        # Photo+ makes it page-sized, which then covers and steals clicks from other items.
        size_pt = float(src_font.get("size", 48)) * float(message.get("scale", 1) or 1)
        em = size_pt * 360 / 72
        w, h = measure_text(content, family, font_path, em)
        pad = em * 0.25
        box = [int(round(w + 2 * pad)), int(round(h + pad))]

        color = src_font.get("color", [0, 0, 0, 255])
        return {
            "content": content,
            "type": "textframe",
            "size": box,
            "fontInfo": {
                "fontName": family or "Meiryo UI",
                "size": round(size_pt, 3),
                "color": color,
                "bold": bool(message.get("fakeBold", False)),
                "italic": False,
                "underline": False,
                "outline": False,
                "outlineColor": [255, 255, 255, 255],
                "shadow": False,
                "shadowInfo": {"blur": 5, "offset": 2, "angle": 135.0, "color": [0, 0, 0, 255]},
                "characterSpacing": int(round(message.get("letterSpacing", 0) or 0)),
                "lineSpacing": 0,
            },
            "center": message.get("center", [0, 0]),
            "angle": message.get("angle", 0),
            "scale": 1.0,
            "layout": message.get("layout", "horizontal"),
            "horizontalAlignment": message.get("horizontalAlignment", "left"),
            "verticalAlignment": message.get("verticalAlignment", "top"),
            "workSpaceNumber": 1,
            "zindex": message.get("zindex", 0),
        }

    def background(self, edited):
        pattern = (edited.get("backgroundData") or {}).get("backgroundPattern") or {}
        names = []
        if pattern.get("type") == "D" and pattern.get("patternName"):
            name = pattern["patternName"]
            names = [name, name.replace("-L-", "-S-", 1), name.replace("-S-", "-L-", 1)]
        custom = pattern.get("customImage") or {}
        if pattern.get("type") == "O" and custom.get("imageName"):
            names = [custom["imageName"]]
        found = [n for n in names if self.add_resource(n)]
        if names and not found:
            self.warnings.append("background %r not found inside the save file" % names[0])


def build_template_root(page_json, template):
    """The page JSON Photo+ expects: template fields + paperSizeList + editedPaperSize."""
    edited = page_json["editedPaperSize"]
    if template:
        root = copy.deepcopy(template)
    else:
        root = {k: v for k, v in page_json.items() if k != "editedPaperSize"}
        size = {k: v for k, v in edited.items()
                if k not in ("photos", "cliparts", "messages", "workData", "disclabel")}
        root["paperSizeList"] = [size]
    root.setdefault("mediaTypeIdList", ["photo"])
    root["version"] = 3
    for key in ("id", "thumbnail", "update", "function", "borderType"):
        if key in page_json:
            root[key] = page_json[key]
    master = copy.deepcopy(root)
    root["editedPaperSize"] = edited
    return root, master


def android_to_etdx(src, dst, resources, use_wpf=True):
    header, files = read_android_save(src)
    warnings = []
    mode = header.get("DataMode")
    if mode not in (0, 20):
        warnings.append("data mode %s (%s) may not be supported by Photo+"
                        % (mode, DATA_MODES.get(mode, "unknown")))

    page_count = int(header.get("TotalPageCount") or 0) or \
        sum(1 for n in files if n.endswith("_info.json"))

    temp_dir = tempfile.mkdtemp(prefix="epson_port_")
    try:
        # Measure every image once (WPF when available, for exact Photo+ geometry).
        image_names = [n for n, d in files.items() if image_extension(d) in (".jpg", ".png")]
        paths = {}
        for n in image_names:
            p = os.path.join(temp_dir, n)
            with open(p, "wb") as f:
                f.write(files[n])
            paths[n] = p
        wpf = image_geometry_wpf(list(paths.values())) if use_wpf else {}
        measured = {}
        for n, p in paths.items():
            key = os.path.normcase(os.path.abspath(p))
            measured[n] = wpf.get(key) or image_geometry_python(files[n])

        taken_ids = set()
        pages, master, fonts = [], None, {}
        for index in range(page_count):
            json_name = "P%04d_info.json" % index
            if json_name not in files:
                warnings.append("missing %s; page skipped" % json_name)
                continue
            page_json = json.loads(files[json_name].decode("utf-8-sig"))
            if "editedPaperSize" not in page_json:
                warnings.append("%s has no edited layout; page skipped" % json_name)
                continue
            template, source = resources.template(page_json.get("id"))
            if template is None:
                warnings.append("template %r not found (pass --apk); paper size is fixed "
                                "to the saved one" % page_json.get("id"))
            converter = PageConverter(files, resources, taken_ids, temp_dir, warnings)
            converter.convert(page_json, template, measured)
            root, page_master = build_template_root(page_json, template)
            master = master or page_master
            fonts.update(converter.fonts)
            pages.append((new_id(taken_ids), root, converter.entries))
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not pages:
        raise FormatError("no pages found in save file")

    project_info = {
        "appVersion": PC_APP_VERSION,
        "editInfo": {"pageEditInfo": {"canAddPage": True, "canCopyPage": True, "canRemovePage": True}},
        "formatInfo": {"saveFormat": 0},
    }
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("page.json", json.dumps([p[0] for p in pages]))
        z.writestr("projectInfo.json", json.dumps(project_info, indent=2))
        dirs = set()
        for page_id, root, entries in pages:
            z.writestr(page_id + "/", b"")
            z.writestr(page_id + "/_info.json",
                       json.dumps(root, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            for rel, data in entries:
                if "/" in rel:
                    d = page_id + "/" + rel.rsplit("/", 1)[0] + "/"
                    if d not in dirs:
                        dirs.add(d)
                        z.writestr(d, b"")
                z.writestr(page_id + "/" + rel, data)
        z.writestr("MasterTemplate/", b"")
        z.writestr("MasterTemplate/_info.json", json.dumps(master, indent=2, ensure_ascii=False))
    return header, [p[0] for p in pages], warnings, fonts


# ---------------------------------------------------------------- commands

def find_apk(*folders):
    for folder in folders:
        hits = sorted(glob.glob(os.path.join(folder, "*.apk")))
        hits = [h for h in hits if "epson" in os.path.basename(h).lower()] or hits
        if hits:
            return hits[0]
    return None


def cmd_info(args):
    header, files = read_android_save(args.file)
    shown = {k: v for k, v in header.items() if k not in ("FileNameList", "FileSizeList")}
    shown["DataModeName"] = DATA_MODES.get(header.get("DataMode"), "unknown")
    print(json.dumps(shown, indent=2, ensure_ascii=False))
    print("\n%d embedded files:" % len(files))
    for name, data in files.items():
        print("  %-60s %10d" % (name, len(data)))


def cmd_extract(args):
    header, files = read_android_save(args.file)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "Header.json"), "w", encoding="utf-8") as f:
        json.dump(header, f, indent=2, ensure_ascii=False)
    for name, data in files.items():
        with open(os.path.join(args.out_dir, os.path.basename(name)), "wb") as f:
            f.write(data)
    print("extracted %d files to %s" % (len(files), args.out_dir))


def cmd_to_etdx(args):
    if os.path.isdir(args.source):
        sources = [os.path.join(args.source, n) for n in sorted(os.listdir(args.source))]
        sources = [p for p in sources if os.path.isfile(p) and looks_like_android_save(p)]
        out_dir = args.output or os.path.join(args.source, "converted")
        jobs = [(s, os.path.join(out_dir, os.path.basename(s) + ".etdx")) for s in sources]
    else:
        out = args.output or (args.source + ".etdx")
        if os.path.isdir(out) or not out.lower().endswith(".etdx"):
            out_dir = out
            out = os.path.join(out, os.path.basename(args.source) + ".etdx")
        else:
            out_dir = os.path.dirname(out) or "."
        jobs = [(args.source, out)]
    if not jobs:
        print("no Epson Creative Print save files found", file=sys.stderr)
        return 1
    os.makedirs(out_dir, exist_ok=True)

    src_dir = args.source if os.path.isdir(args.source) else os.path.dirname(os.path.abspath(args.source))
    apk = args.apk or find_apk(src_dir, os.getcwd(), HERE)
    if apk:
        print("templates and fonts from: %s" % apk)
    else:
        print("no APK found: paper sizes will be fixed to the saved one (use --apk)")
    resources = Resources(apk)

    failed = 0
    all_fonts = {}
    for src, dst in jobs:
        try:
            header, pages, warnings, fonts = android_to_etdx(src, dst, resources, not args.no_wpf)
        except (FormatError, ValueError, KeyError, OSError) as e:
            failed += 1
            print("FAIL %s: %s" % (src, e), file=sys.stderr)
            continue
        all_fonts.update(fonts)
        print("OK   %s -> %s (%d page(s), %s)" % (os.path.basename(src), dst, len(pages),
                                                 DATA_MODES.get(header.get("DataMode"), "unknown")))
        for w in warnings:
            print("     note: " + w)
    if all_fonts:
        font_dir = os.path.join(out_dir, "fonts")
        os.makedirs(font_dir, exist_ok=True)
        for name, data in all_fonts.items():
            with open(os.path.join(font_dir, name), "wb") as f:
                f.write(data)
        print("\nText uses fonts from the Android app, saved to %s." % font_dir)
        print("Install them in Windows (right-click > Install) so Photo+ shows the same lettering.")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info", help="show header and embedded files of an Android save file")
    p.add_argument("file")
    p.set_defaults(func=cmd_info)
    p = sub.add_parser("extract", help="unpack an Android save file into a folder")
    p.add_argument("file")
    p.add_argument("out_dir")
    p.set_defaults(func=cmd_extract)
    p = sub.add_parser("to-etdx", help="convert Android save file(s) to Photo+ .etdx")
    p.add_argument("source", help="save file, or the EpsonCollage folder copied from the phone")
    p.add_argument("-o", "--output", help="output .etdx file or folder (default: <source>/converted)")
    p.add_argument("--apk", help="Epson Creative Print APK (for templates and fonts)")
    p.add_argument("--no-wpf", action="store_true", help="measure images in Python instead of WPF")
    p.set_defaults(func=cmd_to_etdx)
    args = parser.parse_args()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
