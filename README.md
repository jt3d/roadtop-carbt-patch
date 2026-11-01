# rt_carbt_patch.py

Patches the stock RoadTop S7 LT v3.0.1 `GEMINI_PACK.BIN` (BMW NBT/EVO, `QD505-QD517-bmw-incell-S7-LT-2026.04.28-v3.0.1`, MD5 `1ad653bd76be9f19bd2df3beca30baab`) so that with wireless Android Auto:
- calls go through the car's own Bluetooth (iDrive hands-free and the car's microphone), and
- media stays on AUX.

The script refuses to run on any other input file.

## Who this is for
It was written for early BMW F-series cars, typically built before April 2014, that have the **S6NHA "Hands-free with USB interface"** option. That option provides:
- Bluetooth hands-free calling through iDrive, and
- a USB audio interface with an AUX port,

but **no Bluetooth audio streaming (A2DP)**.

With stock firmware, these cars force a bad trade-off:
- **"Use Car's BT Channel" OFF:** media plays over AUX, but calls use the retrofit screen's own microphone.
- **"Use Car's BT Channel" ON:** calls go through iDrive and the car's microphone, but the head unit expects media to stream to the car over Bluetooth. The car can't receive that, so media goes silent.

This patcher gives you both: calls through iDrive and media over AUX.

**If your car already supports A2DP streaming, this patcher is probably not for you.** Turning on "Use Car's BT Channel" in the stock firmware should already send calls and media through the car.

## Builds
| Build | Setting "Use Car's BT Channel" | Status | What it does |
|---|---|---|---|
| **B** (default) | **OFF** | Tested in a car, works | Uses PEMP ZLT's "Use Car's BT Phone Channel" logic. The head unit still uses Bluetooth to start wireless AA, but it no longer takes over the phone's Bluetooth. 3 instructions in `libandroidautomodule.so`. |
| A | ON | Untested | Keeps the ON setting, but media is handled as if it were OFF. 3 instructions across `libandroidautomodule.so` and `libappmcucommunication.so.1.0.0`. |

## Use
Run it on Linux or WSL2, with Python 3.8+ and `squashfs-tools` (`sudo apt install squashfs-tools`). Keep the files on the Linux filesystem, not under `/mnt/c`.

```
python3 rt_carbt_patch.py patch GEMINI_PACK.BIN patched/GEMINI_PACK.BIN          # build B
python3 rt_carbt_patch.py patch GEMINI_PACK.BIN patched/GEMINI_PACK.BIN --build A
python3 rt_carbt_patch.py check patched/GEMINI_PACK.BIN                            # checksum validation of any pack
```

The build is deterministic. With squashfs-tools 4.7.5 the output MD5 is:
- B: `1203ce88e980b4d670aca3cd51a8535c`
- A: `3995452c402c9aea7ee343a2ce708bb4`

Other squashfs-tools versions may produce a different MD5 with the same contents. The script reports this, and its verification still applies.

## What it checks
1. The input is exactly the stock image, and all of its checksums are valid.
2. Repacking the unmodified spapp filesystem reproduces stock byte-for-byte. This proves your squashfs-tools are compatible.
3. Each patched file is the stock file, and the original bytes are present at every patch offset.
4. The output passes every check:
   - payload MD5
   - component MD5 table
   - per-2 MB MD5 checks in the update script
   - uImage CRCs
5. Nothing outside the spapp region and its checksums has changed.
6. The rebuilt spapp, unpacked again, differs from stock only in the patched files, at exactly the patched bytes.

## Flashing
1. Put only `GEMINI_PACK.BIN` on a FAT32 USB stick, in the root, with no other firmware files on it.
2. Go to Settings > System > Firmware Update > **System Update**.
3. Rules:
   - **Never** flash `upgrade.bin` (the MCU).
   - Don't put `ISPBOOOT.BIN` on the same stick.
   - Keep the stock files as your recovery image.

You use this at your own risk.

The GEMINI_PACK format handling is based on GeminiTop's firmware_tools (MIT License, Copyright (c) 2026 Mart).
