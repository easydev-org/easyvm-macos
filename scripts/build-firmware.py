#!/usr/bin/env python3
"""BUILD THE THREE FILES A macOS GUEST BOOTS THROUGH: boot.img, macos.rom, macos.vars.

THIS USED TO RUN INSIDE `easyvm run`, AS A `fetch: opencore` STEP. It is a CI build now, for
two reasons:

  1. THE OUTPUT IS A PURE FUNCTION OF FIVE PINNED INPUTS. The same OpenCore archive, template
     ISO, OVMF code and vars, and recovery image always produce the same tree; only the
     identity is random, and that is now deliberately shared (see IDENTITY below). So building
     it once and publishing the result turns a thirty-second step in every macOS stage into a
     download.

  2. THE BUILD IS NOT A GUEST ACTION. It never boots anything, never types anything, and needs
     no screen. It was written as a step because that was the only way to run code at the time,
     and a step that does host work is a step whose failure reads like a guest failure.

WHAT IT DOES, in the order the measurements require:

  7z   extract the EFI tree from the LongQT template (it supplies the kexts, ACPI tables and
       config.plist; OpenCore's own archive is the bootloader and nothing else)
  cp   merge that tree over OpenCore's, keeping OpenCore's bootloader
  macserial  generate the serial and board serial, because their last characters are a
       checksum over the model - a made-up one fails Apple's check and the guest stops booting
  plist  write the identity, SIP off, no picker, and 1920x1080 into the template's config
  7z   extract apfs.efi out of the recovery image, and register it in the config
  mtools  write a GPT disk whose one partition is the ESP, and copy the tree into it

THE GPT IS NOT OPTIONAL AND ITS ABSENCE COST FIVE RUNS. `mformat -F` on the whole file makes a
"superfloppy": FAT at LBA 0 with an MBR signature written over the FAT boot sector. mtools
reads it, so every check on this side passed; OVMF did not, because firmware looks for an EFI
SYSTEM PARTITION in a partition table and a filesystem at LBA 0 is neither.
"""

import argparse
import hashlib
import os
import plistlib
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid
import zlib
from datetime import datetime, timezone

# --- constants, all measured --------------------------------------------------------------

BOOT_IMAGE_MB = 256
SECTOR_SIZE = 512
ESP_SECTORS_PER_CLUSTER = 4
ESP_START_SECTOR = 2048          # 1 MiB in: conventional, and cluster-aligned
GPT_ENTRY_ARRAY_SECTORS = 32     # 128 entries * 128 bytes = 16 KiB
GUEST_WIDTH, GUEST_HEIGHT = 1920, 1080

APFS_DRIVER_PATH = "macOS Base System/usr/standalone/i386/apfs.efi"
MACSERIAL_MEMBER = "Utilities/macserial/macserial.linux"

# THE FIVE INPUTS, BY THE TAG THEY ARE PUBLISHED UNDER AND THE SHA256 THIS BUILD EXPECTS.
#
# THE PIN IS CHECKED HERE EVEN THOUGH THE WORKFLOW DOWNLOADED THE FILE: the registry guarantees
# the bytes it stored, not that they are the bytes this code was written against, and a tag can
# be overwritten. The failure names the pin rather than a transfer.
ASSETS = {
    "opencore": {
        "file": "opencore.zip",
        "tag": "macos-asset-opencore",
        "sha256": "2ffab6ebf58c7aefb0bcb3a1a385d207746823d6dd87d44bd666e1286939943e",
    },
    "opencore-template": {
        "file": "opencore-template.iso",
        "tag": "macos-asset-opencore-template",
        "sha256": "287328995d4198f1b05166f087d85bf7ef66bedafe150d17ad112ac8de60051d",
    },
    "ovmf-code": {
        "file": "OVMF_CODE.fd",
        "tag": "macos-asset-ovmf-code",
        "sha256": "52a625db5a45329bf8005232db33e94781787d92ae8d088f2f73332dd4ded3a9",
    },
    "ovmf-vars-1920x1080": {
        "file": "OVMF_VARS-1920x1080.fd",
        "tag": "macos-asset-ovmf-vars-1920x1080",
        "sha256": "6ed987af3a3c155be71665f510eae3e007eda9b8b94afd59d45e91c4a11565cc",
    },
    "recovery": {
        "file": "recovery.dmg",
        "tag": None,   # named by release: macos-recovery-sequoia / -tahoe
        "sha256": None,  # the recovery image is mirrored, not pinned to a digest here
    },
}

RECOVERY_TAGS = {"15": "macos-recovery-sequoia", "26": "macos-recovery-tahoe"}


class BuildError(Exception):
    pass


def run(argv, what=None):
    """Run a command and fail with its first line of output, which is where the reason is."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        raise BuildError(f"{argv[0]} is not installed")
    if out.returncode != 0:
        first = (out.stderr or out.stdout).strip().split("\n")[0]
        raise BuildError(f"{what or argv[0]}: {first}")
    return out.stdout


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def asset(assets_dir, name):
    """Find one pinned input and prove it is the bytes this build expects."""
    spec = ASSETS[name]
    path = os.path.join(assets_dir, spec["file"])
    if not os.path.exists(path):
        raise BuildError(
            f"no {spec['file']} in {assets_dir}. The workflow pulls {spec['tag'] or name} "
            f"before this runs, and pull-artifact.sh is what does it - `oras pull` has no resume"
        )
    if spec["sha256"]:
        got = sha256_file(path)
        if got != spec["sha256"]:
            raise BuildError(
                f"{spec['file']}: sha256 {got} but the pin says {spec['sha256']}. Either "
                f"{spec['tag']} was overwritten or the pin is stale - and an OpenCore that "
                f"silently upgraded produces a disk that boots differently"
            )
    return path


# --- the template --------------------------------------------------------------------------


def find_efi_tree(root):
    """The EFI directory that holds OC/OpenCore.efi, preferring EFI_RELEASE over EFI_DEBUG.

    THE CHOICE IS BY NAME, not by taking the first match: a directory walk returns whatever
    order the filesystem gives, and the debug build is slower and writes a log nothing reads.
    """
    candidates = []
    for dirpath, dirnames, _ in os.walk(root):
        for d in dirnames:
            if d.lower() != "oc":
                continue
            oc = os.path.join(dirpath, d)
            if not os.path.exists(os.path.join(oc, "OpenCore.efi")):
                continue
            parent = os.path.dirname(oc)
            if os.path.basename(parent).lower() == "efi":
                candidates.append(parent)
    if not candidates:
        raise BuildError("holds no EFI/OC/OpenCore.efi; the template layout changed")
    for c in candidates:
        if "EFI_RELEASE" in c.upper():
            return c
    return candidates[0]


def extract_template(template_iso, dest):
    """Unpack the template ISO and return the EFI tree inside it.

    7Z AND NOT AN ISO READER, because the image carries two name tables: the ISO9660 one is
    8.3 uppercase with a `;1` suffix (`CONFIG.PLIST;1`), and the Joliet one has the names the
    config refers to (`config.plist`, `Lilu.kext`). Extracting through the 9660 table produces
    a tree whose names match nothing OpenCore looks for.
    """
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    out = os.path.join(dest, "extract")
    os.makedirs(out)
    run(["7z", "x", "-y", f"-o{out}", template_iso],
        what=f"unpacking {os.path.basename(template_iso)}")
    chosen = find_efi_tree(out)
    for required in ["ACPI", "Kexts", "Drivers", "Resources"]:
        p = os.path.join(chosen, "OC", required)
        if not os.path.isdir(p):
            raise BuildError(
                f"{template_iso} has no OC/{required} under its EFI tree; the guest would "
                f"boot to a prohibited sign with nothing in any log to explain it"
            )
    if not os.path.exists(os.path.join(chosen, "OC", "config.plist")):
        raise BuildError(f"{template_iso} has no OC/config.plist; the template layout changed")
    return chosen


def copy_tree(src, dst):
    """Copy src's CONTENTS into dst, which is what `cp -r src/. dst` means."""
    os.makedirs(dst, exist_ok=True)
    for entry in os.listdir(src):
        s = os.path.join(src, entry)
        d = os.path.join(dst, entry)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)


def unpack_opencore(archive, work, dest):
    """Extract OpenCore's payload and its serial generator into dest/EFI (+ dest/macserial).

    TWO PATTERNS, BECAUSE THE ARCHIVE HOLDS TWO TREES THIS BUILD NEEDS AND ONE IT DOES NOT:
    X64/EFI is the payload, Utilities/macserial/* is the generator, and IA32/ and Docs/ are
    excluded on purpose - this guest is x86-64 and the documentation would go into the image.
    """
    stage = os.path.join(work, "opencore-unpacked")
    if os.path.exists(stage):
        shutil.rmtree(stage)
    run(["7z", "x", "-y", f"-o{stage}", archive, "X64/*", "Utilities/macserial/*"],
        what=f"unpacking {os.path.basename(archive)}")
    source = os.path.join(stage, "X64", "EFI")
    if not os.path.isdir(source):
        raise BuildError("the OpenCore archive has no X64/EFI tree; its release layout changed")
    copy_tree(source, os.path.join(dest, "EFI"))
    serial = os.path.join(stage, MACSERIAL_MEMBER)
    if not os.path.exists(serial):
        raise BuildError(
            f"the OpenCore archive has no {MACSERIAL_MEMBER}. The generator travels inside "
            f"that archive rather than being a second download"
        )
    shutil.copy2(serial, os.path.join(work, "macserial"))
    os.chmod(os.path.join(work, "macserial"), 0o755)


def merge_template(efi_dir, template_tree):
    """Copy the template's tree over OpenCore's, keeping OpenCore's bootloader.

    THE TARGET IS `efi_dir/EFI`, NOT `efi_dir`: the template tree already ends in `/EFI`, and
    copy_tree puts its CONTENTS into the target. Copying to `efi_dir` would put BOOT and OC at
    `efi_dir/BOOT` while UnpackOpencore wrote them at `efi_dir/EFI/BOOT` - a tree with
    OC/OpenCore.efi in it and NO EFI/OC/config.plist, so OpenCore answers
    `OC: Failed to load configuration!` MEASURED on a macOS 26 guest built that way.
    """
    copy_tree(template_tree, os.path.join(efi_dir, "EFI"))
    for must in [("EFI", "OC", "OpenCore.efi"), ("EFI", "OC", "config.plist"),
                 ("EFI", "BOOT", "BOOTx64.efi")]:
        p = os.path.join(efi_dir, *must)
        if not os.path.exists(p):
            raise BuildError(
                f"the merged tree at {efi_dir} has no {'/'.join(must)}: the firmware opens "
                f"\\EFI\\BOOT\\BOOTX64.EFI, that bootloader reads \\EFI\\OC\\config.plist, and a "
                f"tree missing either stops at `OC: Failed to load configuration!`"
            )


# --- the identity --------------------------------------------------------------------------


def make_identity(work, release, model):
    """Serial, board serial, UUID and MAC, generated once and KEPT.

    MACOS BINDS AN INSTALLATION TO IT: a new serial per boot gives a disk that boots on the
    machine that made it and on nothing else. This build runs once in CI and the identity is
    shared by every guest that uses the artifact - a deliberate choice, because a uuid from
    `random` would make every rebuild a different machine.
    """
    path = os.path.join(work, "identity.json")
    if os.path.exists(path):
        import json
        with open(path) as f:
            return json.load(f)

    serial, board = generate_serial(os.path.join(work, "macserial"), model)
    ident = {
        "release": release,
        "model": model,
        "serial": serial,
        "board_serial": board,
        "uuid": str(uuid.uuid4()).upper(),
        "mac": "00:16:CB:%02X:%02X:%02X" % (
            random.randrange(0, 256), random.randrange(0, 256),
            random.randrange(0, 256)),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    import json
    with open(path, "w") as f:
        json.dump(ident, f, indent=2)
    return ident


def generate_serial(macserial, model):
    """Run macserial, which knows Apple's checksums.

    A HAND-ROLLED SERIAL LOOKS RANDOM BUT IS NOT: its last characters are a checksum over the
    model and the factory code, and macOS derives the board serial from the same data. A made-up
    one fails Apple's check and the guest stops booting when the NVRAM remembers it.
    """
    if not os.path.exists(macserial):
        raise BuildError(f"macserial is not at {macserial}")
    out = run([macserial, "--num", "1", "--model", model], what="macserial")
    # THE OUTPUT IS `<serial> | <board serial>` AND IT MAY BE PRECEDED BY A WARNING.
    #
    # MEASURED with OpenCore 1.0.7's macserial.linux on linux/amd64:
    #
    #	Warning: arc4random is not available!
    #	C02VW0HEHX87 | C02752501QXJG36JC
    #
    # so the line is the one holding the separator, and the two halves are split on it - not on
    # whitespace, which would take the `|` for a board serial.
    for line in out.splitlines():
        if "|" not in line:
            continue
        left, _, right = line.partition("|")
        serial, board = left.strip(), right.strip()
        if serial and board:
            return serial, board
    raise BuildError(
        f"macserial printed no `<serial> | <board serial>` line; its output was "
        f"{out.strip()[:200]!r}. The format changed, and a made-up serial fails Apple's check"
    )


def mac_bytes(mac):
    """'00:16:CB:AA:BB:CC' -> six raw bytes, which is how OpenCore hands the MAC to macOS."""
    return bytes(int(p, 16) for p in mac.replace("-", ":").split(":"))


def write_config(efi_dir, template_tree, ident, release):
    """Merge the identity into the template's config.plist.

    ONLY THE FIELDS THIS BUILD DEPENDS ON ARE CHANGED. The template's config carries device
    properties and kext lists that were arrived at by somebody else's runs - the GPU and SMC
    properties in particular are not guessable - so replacing the file would throw those away.
    """
    source = os.path.join(template_tree, "OC", "config.plist")
    if not os.path.exists(source):
        raise BuildError("the template has no config.plist under its OC directory")
    with open(source, "rb") as f:
        cfg = plistlib.load(f)

    generic = cfg.get("PlatformInfo", {}).get("Generic")
    if generic is None:
        raise BuildError("the template's config has no PlatformInfo/Generic; the layout changed")
    generic["SystemSerialNumber"] = ident["serial"]
    generic["MLB"] = ident["board_serial"]
    generic["SystemUUID"] = ident["uuid"]
    generic["ROM"] = mac_bytes(ident["mac"])

    # SIP OFF: a 4-byte little-endian bitmask, and 0x7f disables every protection - which the
    # sandbox image needs for its TCC and xcode work.
    nvram = (cfg.get("NVRAM", {}).get("Add", {})
             .get("7C436110-AB2A-4BBB-A880-FE41995C9F82"))
    if nvram is not None:
        nvram["csr-active-config"] = b"\x7f\x00\x00\x00"

    # NO PICKER. A stage boots one thing: the installer. A picker with a two-second timeout shows
    # a menu that has to time out before anything happens - and if a key arrives while it is up,
    # it stops there instead.
    boot_cfg = cfg.get("Misc", {}).get("Boot")
    if boot_cfg is not None:
        boot_cfg["ShowPicker"] = False
        boot_cfg["Timeout"] = 0

    # THE SCREEN SIZE IS NOT DECORATION - EVERY COORDINATE IN A WALKTHROUGH DEPENDS ON IT.
    # The template ships Resolution EMPTY, and an empty one made the guest run at 1280x800 while
    # every click was measured at 1920x1080. RESOLUTION ONLY: ForceResolution is left alone,
    # because 1920x1080 is what the pinned OVMF_VARS template was built for, so this is a matched
    # pair rather than one setting being imposed.
    output = cfg.get("UEFI", {}).get("Output")
    if output is not None:
        output["Resolution"] = f"{GUEST_WIDTH}x{GUEST_HEIGHT}@32"

    target = os.path.join(efi_dir, "EFI", "OC", "config.plist")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as f:
        plistlib.dump(cfg, f)

    # READ BACK RATHER THAN TRUSTING THE WRITE. This file is the whole difference between a guest
    # that boots and one that stops at a prohibited sign.
    with open(target, "rb") as f:
        check = plistlib.load(f)
    got = check.get("PlatformInfo", {}).get("Generic", {}).get("SystemSerialNumber")
    if got != ident["serial"]:
        raise BuildError(
            f"{target} does not hold the serial after writing; the identity did not land and "
            f"the guest would boot with another machine's"
        )
    return target


def install_apfs_driver(efi_dir, recovery, work):
    """Pull Apple's APFS driver out of the recovery image and register it.

    WITHOUT IT OPENCORE CANNOT READ AN APFS PARTITION, and the symptom is silent: the
    installation completes and the picker lists nothing. `EnableJumpstart` does not help - it
    HOOKS a driver the firmware is expected to have, and OVMF has none.
    """
    source = os.path.join(work, "apfs.efi")
    if not os.path.exists(source):
        if not os.path.exists(recovery):
            raise BuildError(f"no recovery image at {recovery}, and the APFS driver can only "
                             f"come from one")
        extract = os.path.join(work, "apfs-extract")
        if os.path.exists(extract):
            shutil.rmtree(extract)
        # 7z walks the DMG's compressed blocks and its HFS+ contents, which is the only way to
        # reach a file inside a UDIF image without mounting it.
        run(["7z", "x", "-y", f"-o{extract}", recovery, APFS_DRIVER_PATH],
            what=f"extracting {APFS_DRIVER_PATH} from {os.path.basename(recovery)}")
        found = ""
        for dirpath, _, filenames in os.walk(extract):
            for name in filenames:
                if name.lower() == os.path.basename(APFS_DRIVER_PATH).lower():
                    found = os.path.join(dirpath, name)
                    break
            if found:
                break
        if not found:
            raise BuildError(
                f"{recovery} holds no {APFS_DRIVER_PATH}; the recovery layout changed. Without "
                f"it OpenCore cannot read the installed system"
            )
        shutil.copy2(found, source)

    # A DRIVER THAT IS NOT A PE IMAGE IS NOT A DRIVER. Cheap, and it is the difference between
    # "the extraction produced a file" and "the extraction produced the file".
    with open(source, "rb") as f:
        head = f.read(2)
    if head != b"MZ":
        raise BuildError(f"{source} is not a PE image (begins {head!r}, expected 'MZ')")

    drivers = os.path.join(efi_dir, "EFI", "OC", "Drivers")
    os.makedirs(drivers, exist_ok=True)
    shutil.copy2(source, os.path.join(drivers, "apfs.efi"))

    config = os.path.join(efi_dir, "EFI", "OC", "config.plist")
    with open(config, "rb") as f:
        cfg = plistlib.load(f)
    uefi = cfg.get("UEFI")
    if uefi is None:
        raise BuildError("the config has no UEFI section; apfs.efi cannot be registered")
    drivers_list = uefi.get("Drivers")
    if not isinstance(drivers_list, list):
        drivers_list = []
        uefi["Drivers"] = drivers_list

    already = any(isinstance(e, dict) and str(e.get("Path", "")).lower() == "apfs.efi"
                  for e in drivers_list)
    if not already:
        # APPENDED RATHER THAN PREPENDED: the drivers OpenCore loads first are the ones it needs
        # for the filesystem it is about to read - this one - so it goes last.
        drivers_list.append({
            "Arguments": "",
            "Comment": "Apple's APFS driver, from the recovery image",
            "Enabled": True,
            "LoadEarly": False,
            "Path": "apfs.efi",
        })
        with open(config, "wb") as f:
            plistlib.dump(cfg, f)

    with open(config, "rb") as f:
        check = plistlib.load(f)
    enabled = any(
        isinstance(e, dict) and str(e.get("Path", "")).lower() == "apfs.efi"
        and e.get("Enabled") is True
        for e in check.get("UEFI", {}).get("Drivers", [])
    )
    if not enabled:
        raise BuildError(f"apfs.efi did not land in {config} as an enabled driver")


# --- the boot image ------------------------------------------------------------------------


def walk_tree(root):
    """Every directory and file under root as slash paths, parents before children.

    THE ORDER MATTERS: `mmd ::/EFI/BOOT` fails before `mmd ::/EFI` has run, so a parent must
    exist before a child is named.
    """
    dirs, files = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        rel = os.path.relpath(dirpath, root)
        for d in sorted(dirnames):
            p = d if rel == "." else f"{rel}/{d}"
            dirs.append(p.replace(os.sep, "/"))
        for name in sorted(filenames):
            p = name if rel == "." else f"{rel}/{name}"
            files.append(p.replace(os.sep, "/"))
    return dirs, files


def write_gpt(path, esp_first, esp_last, total_sectors):
    """A protective MBR, a GPT header with a CRC32, the entry array, and its backup.

    THIS IS WHAT MAKES THE DISK ENUMERABLE. A FAT filesystem at LBA 0 is a "superfloppy": mtools
    reads it and firmware does not, because firmware looks for an EFI SYSTEM PARTITION in a
    partition table.
    """
    esp_type = uuid.UUID("C12A7328-F81F-11D2-BA4B-00A0C93EC93B").bytes_le
    unique = bytearray(os.urandom(16))
    unique[7] = (unique[7] & 0x0F) | 0x40   # version 4
    unique[8] = (unique[8] & 0x3F) | 0x80   # variant
    disk_guid = os.urandom(16)

    entries = bytearray(GPT_ENTRY_ARRAY_SECTORS * SECTOR_SIZE)
    entries[0:16] = esp_type
    entries[16:32] = unique
    struct.pack_into("<Q", entries, 32, esp_first)
    struct.pack_into("<Q", entries, 40, esp_last)
    name = "EFI".encode("utf-16-le")
    entries[56:56 + len(name)] = name

    def header(my_lba, alt_lba, entry_lba):
        h = bytearray(SECTOR_SIZE)
        h[0:8] = b"EFI PART"
        struct.pack_into("<I", h, 8, 0x00010000)
        struct.pack_into("<I", h, 12, 92)
        struct.pack_into("<Q", h, 24, my_lba)
        struct.pack_into("<Q", h, 32, alt_lba)
        struct.pack_into("<Q", h, 40, 34)
        struct.pack_into("<Q", h, 48, total_sectors - 34)
        h[56:72] = disk_guid
        struct.pack_into("<Q", h, 72, entry_lba)
        struct.pack_into("<I", h, 80, 128)
        struct.pack_into("<I", h, 84, 128)
        struct.pack_into("<I", h, 88, zlib.crc32(entries) & 0xFFFFFFFF)
        struct.pack_into("<I", h, 16, zlib.crc32(h[:92]) & 0xFFFFFFFF)
        return h

    backup_entries_lba = total_sectors - 1 - GPT_ENTRY_ARRAY_SECTORS
    primary = header(1, total_sectors - 1, 2)
    backup = header(total_sectors - 1, 1, backup_entries_lba)

    mbr = bytearray(SECTOR_SIZE)
    mbr[0x1BE + 4] = 0xEE
    mbr[0x1BE + 5:0x1BE + 8] = b"\xff\xff\xff"
    struct.pack_into("<I", mbr, 0x1BE + 8, 1)
    struct.pack_into("<I", mbr, 0x1BE + 12, min(total_sectors - 1, 0xFFFFFFFF))
    mbr[0x1FE] = 0x55
    mbr[0x1FF] = 0xAA

    with open(path, "r+b") as f:
        f.write(mbr)
        f.seek(SECTOR_SIZE)
        f.write(primary)
        f.seek(2 * SECTOR_SIZE)
        f.write(entries)
        f.seek(backup_entries_lba * SECTOR_SIZE)
        f.write(entries)
        f.seek((total_sectors - 1) * SECTOR_SIZE)
        f.write(backup)


def check_gpt(path, esp_first):
    """READ THE TABLE BACK, the way firmware will."""
    with open(path, "rb") as f:
        f.seek(SECTOR_SIZE)
        h = f.read(SECTOR_SIZE)
        if h[0:8] != b"EFI PART":
            raise BuildError(f"{path}: LBA 1 is not a GPT header")
        f.seek(2 * SECTOR_SIZE)
        entries = f.read(GPT_ENTRY_ARRAY_SECTORS * SECTOR_SIZE)
        first = struct.unpack_from("<Q", entries, 32)[0]
        if first != esp_first:
            raise BuildError(f"{path}: the GPT says the ESP starts at LBA {first}, "
                             f"not {esp_first}")


def check_esp(path, esp_first):
    """AND THE FILESYSTEM INSIDE IT, which mtools never looks at."""
    partition = f"{path}@@{esp_first * SECTOR_SIZE}"
    out = run(["mdir", "-i", partition, "-/", "::/"], what="reading the ESP")
    if "EFI" not in out.upper():
        raise BuildError(f"{path}: the ESP has no EFI directory (mdir listed {out!r})")


def build_boot_image(efi_dir, dest):
    image_size = BOOT_IMAGE_MB * 1024 * 1024
    total_sectors = image_size // SECTOR_SIZE
    esp_first = ESP_START_SECTOR
    esp_last = total_sectors - 34
    esp_sectors = esp_last - esp_first + 1

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest):
        os.remove(dest)
    with open(dest, "wb") as f:
        f.truncate(image_size)

    for tool in ["mformat", "mcopy", "mdir", "mmd"]:
        if shutil.which(tool) is None:
            raise BuildError(f"{tool} is not installed. The boot image is a FAT32 ESP built "
                             f"with mtools, and there is no second way to write one")

    # THE FAT GOES INSIDE THE PARTITION, at its byte offset. mtools addresses a file offset with
    # the @@ suffix, which writes the same bytes a mount would.
    partition = f"{dest}@@{esp_first * SECTOR_SIZE}"
    run(["mformat", "-F", "-i", partition, "-c", str(ESP_SECTORS_PER_CLUSTER),
         "-T", str(esp_sectors), "-v", "EFI"], what="mformat")

    # TWO PASSES: mmd for every directory, then mcopy for every file.
    #
    # `mcopy -s <dir> ::/<x>` DOES NOT PUT <dir> INSIDE <x>; it copies <dir>'s CONTENTS there.
    # MEASURED with GNU mtools 4.0.48: `mcopy -s EFI ::/` lists `BOOTX64 EFI` at the root and
    # `mdir ::/EFI/BOOT` answers File not found. So an image built that way has no EFI/BOOT at
    # all, and the firmware opens \EFI\BOOT\BOOTX64.EFI and drops to its shell.
    dirs, files = walk_tree(efi_dir)
    for d in dirs:
        run(["mmd", "-i", partition, "::/" + d], what=f"mmd ::/{d}")
    for f in files:
        src = os.path.join(efi_dir, f.replace("/", os.sep))
        run(["mcopy", "-i", partition, "-m", src, "::/" + f], what=f"mcopy ::/{f}")

    write_gpt(dest, esp_first, esp_last, total_sectors)
    check_gpt(dest, esp_first)
    check_esp(dest, esp_first)

    # THE PATH THE FIRMWARE ACTUALLY OPENS, checked by a directory listing and a folded
    # comparison because FAT is case-insensitive: EDK II's FAT driver upper-cases both the entry
    # and the path it is asked for.
    for required, name in [("::/EFI/BOOT", "BOOTX64.EFI"), ("::/EFI/OC", "OPENCORE.EFI")]:
        out = run(["mdir", "-i", partition, "-/", required],
                  what=f"reading {required}")
        if name.lower() not in out.lower():
            raise BuildError(
                f"{dest}: {required} does not contain {name} (mdir listed {out.strip()[:120]!r}). "
                f"The firmware opens \\EFI\\BOOT\\BOOTX64.EFI and a case fold is how FAT matches it"
            )


# --- main ----------------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--assets", required=True,
                   help="directory holding the five pinned inputs")
    p.add_argument("--release", default="15", help="macOS release, 15 or 26")
    p.add_argument("--model", default="iMacPro1,1", help="the SMBIOS model macserial uses")
    p.add_argument("--out", required=True, help="where boot.img, macos.rom and macos.vars go")
    args = p.parse_args()

    work = tempfile.mkdtemp(prefix="firmware-")
    try:
        assets = {name: asset(args.assets, name) for name in
                  ["opencore", "opencore-template", "ovmf-code", "ovmf-vars-1920x1080", "recovery"]}

        template_tree = extract_template(assets["opencore-template"],
                                         os.path.join(work, "template"))
        efi_dir = os.path.join(work, "efi")
        os.makedirs(efi_dir)
        unpack_opencore(assets["opencore"], work, efi_dir)
        merge_template(efi_dir, template_tree)

        ident = make_identity(work, args.release, args.model)
        write_config(efi_dir, template_tree, ident, args.release)
        install_apfs_driver(efi_dir, assets["recovery"], work)

        os.makedirs(args.out, exist_ok=True)
        boot = os.path.join(args.out, "boot.img")
        build_boot_image(efi_dir, boot)

        # THE ROM AND THE VARS ARE THE PINNED INPUTS, RENAMED TO WHAT THE BOOT STEP OPENS. The
        # vars file is named `.template` because qemu WRITES to it and each guest needs its own
        # copy - see internal/asset's fetch, which copies it once per work directory.
        rom = os.path.join(args.out, "macos.rom")
        shutil.copy2(assets["ovmf-code"], rom)
        vars_template = os.path.join(args.out, "macos.vars.template")
        shutil.copy2(assets["ovmf-vars-1920x1080"], vars_template)

        for f, what in [(boot, "boot.img"), (rom, "macos.rom"), (vars_template, "macos.vars")]:
            size = os.path.getsize(f)
            print(f"  {what}: {size} bytes")
        print(f"  identity: {ident['serial']} / {ident['mac']}")
        return 0
    except BuildError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
