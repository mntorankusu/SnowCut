"""settings_handler.py: CM110 firmware implementation of opcode 0xC5 (SETTINGS gateway).

Provides:
  - Opcode 0xC5 handler for USB BOT SCSI:
      CDB[0]   = 0xC5
      CDB[1]   = flag (0x00 = GET, 0x01 = SET with EEPROM commit, 0x02 = SET RAM only)
      CDB[2]   = setting_key (0x00 = ALL settings block, >0 = individual setting)
      CDB[3]   = reserved / sub-flags
      CDB[4:8] = value (32-bit big-endian integer) when storing a setting
  - Python settings dictionary definitions and parser for the 64-byte settings block.
"""
import struct

SETTINGS_SYMBOLS = dict(
    scsi_check       = 0x88f9361c, # (dir, len)
    scsi_send        = 0x88f94204, # (src, 0x1400, reqlen, total)
    scsi_complete    = 0x88f940c4, # () -> CSW GOOD (status 0)
    scsi_fail        = 0x88f94040, # (sense_idx) -> CSW FAILED (status 1)
    scsi_buf         = 0x8820f820, # BOT 5120B bulk staging buffer
    settings_ram     = 0x881cb720, # RAM address of device settings struct (64 bytes)
    eeprom_write     = 0x88cda4ec, # WriteByteBoth(a0=eeprom_addr, a1=val, a2=0)
    eeprom_read      = 0x88cda26c, # ReadByteMain(a0=eeprom_addr, a1=0)
)

# Settings definitions: key -> RAM offset in the 64-byte block, size, EEPROM byte, name.
# Checked against the stock firmware (v1819W): the boot loader FUN_88d3eb30 (EEPROM -> RAM,
# with its range checks), the touch-panel setters in SettingsMode.c (0x88d3b6a0..0x88d3c918)
# and the code that consumes each field:
#   0x12 -> +0x05 is the key-beep switch: the beep routine FUN_88e0124c returns early when it
#        is 0 (FUN_88d3ba90). Nonzero reads as 1 at boot. (Earlier notes called it "cut area".)
#   0x22 -> +0x28 is the auto power-off switch: FUN_88cdae6c only times out when it is set.
#        The panel writes 0xFF / 0, and any nonzero byte is on. (Earlier notes called it "buzzer".)
#   0x18 -> +0x20 is the auto power-off delay in hours: timeout = +0x24 * +0x20 (FUN_88d3bab0).
#   0x24 -> +0x24 is NOT a free timeout: boot sets it to 60 if EEPROM 0x24 == 1 (a factory
#        minutes mode), else 3600. Writing seconds here also writes one byte to EEPROM 0x24.
#   0x10 -> +0x04 unit: 0 = mm (FUN_88d3c4b4 divides 0.01 mm by 100), nonzero = inch.
#   0x23 -> +0x30 language 1..10, in the order of SettingsMode.c's name table. The panel also
#        reloads the UI text (FUN_88d02104). 0xC5 does not, so the change takes effect after a restart.
#   0x11 -> +0x08: the panel offers 0 / 1 / 2 (0xFF reads as 0). It selects a constant in the
#        layout code FUN_88d262e4 (1 -> 25.0, 2 -> 178.0). Meaning not decoded.
#   0x13 -> +0x00 (word, +0x02 is its low half): 0..9, 9 only on some hardware (FUN_88d403a8).
#        The panel's setter FUN_88d3dbbc also applies it (FUN_88c85364 / 88c85468), but 0xC5 only
#        stores it. Meaning not decoded. (Earlier notes called it "seam allowance".)
# Boot range checks: speeds 1..5 (else 3), pressures 1..19 (else 10), hours 1..4 (else 1).
SETTINGS_DEFS = {
    0x01: {"ram_off": 0x1C, "size": 1, "eeprom": 0x14, "name": "cut_speed",          "desc": "Cut speed (1-5)"},
    0x02: {"ram_off": 0x1D, "size": 1, "eeprom": 0x15, "name": "cut_pressure",       "desc": "Cut pressure (1-19, 10 = middle)"},
    0x03: {"ram_off": 0x1E, "size": 1, "eeprom": 0x16, "name": "draw_speed",         "desc": "Draw speed (1-5)"},
    0x04: {"ram_off": 0x1F, "size": 1, "eeprom": 0x17, "name": "draw_pressure",      "desc": "Draw pressure (1-19, 10 = middle)"},
    0x05: {"ram_off": 0x20, "size": 1, "eeprom": 0x18, "name": "auto_off_hours",     "desc": "Auto power-off delay in hours (1-4)"},
    0x06: {"ram_off": 0x28, "size": 4, "eeprom": 0x22, "name": "auto_off",           "desc": "Auto power-off (0=off, 1=on)"},
    0x07: {"ram_off": 0x04, "size": 1, "eeprom": 0x10, "name": "unit",               "desc": "Unit of measurement (0=mm, 1=inch)"},
    0x08: {"ram_off": 0x08, "size": 4, "eeprom": 0x11, "name": "layout_mode",        "desc": "Layout mode 0-2 (meaning not decoded)"},
    0x09: {"ram_off": 0x30, "size": 1, "eeprom": 0x23, "name": "language",           "desc": "Language (1-10, see LANGUAGE_NAMES)"},
    0x0A: {"ram_off": 0x02, "size": 2, "eeprom": 0x13, "name": "level_0x13",         "desc": "EEPROM 0x13 level 0-9 (meaning not decoded, takes effect after restart)"},
    0x0B: {"ram_off": 0x05, "size": 1, "eeprom": 0x12, "name": "sound",              "desc": "Key beep (0=off, 1=on)"},
    0x0C: {"ram_off": 0x24, "size": 4, "eeprom": 0x24, "name": "auto_off_base_sec",  "desc": "Auto power-off base seconds (3600, or 60 in the factory mode set by EEPROM byte 1)"},
}

# Alias by direct EEPROM address for caller convenience
EEPROM_TO_LOGICAL_KEY = {
    0x10: 0x07, # unit
    0x11: 0x08, # layout_mode
    0x12: 0x0B, # sound
    0x13: 0x0A, # level_0x13
    0x14: 0x01, # cut_speed
    0x15: 0x02, # cut_pressure
    0x16: 0x03, # draw_speed
    0x17: 0x04, # draw_pressure
    0x18: 0x05, # auto_off_hours
    0x22: 0x06, # auto_off
    0x23: 0x09, # language
    0x24: 0x0C, # auto_off_base_sec
}

# The firmware's own names (SettingsMode.c string table at 0x8900a4c8, 0x38 bytes apart)
LANGUAGE_NAMES = {
    1: "English",
    2: "Deutsch (German)",
    3: "Français (French)",
    4: "Nederlands (Dutch)",
    5: "Italiano (Italian)",
    6: "Español (Spanish)",
    7: "Русский (Russian)",
    8: "日本語 (Japanese)",
    9: "Português (Portuguese)",
    10: "简体中文 (Simplified Chinese)",
}


def setting_value(raw_64b: bytes, key: int) -> int:
    """One setting's value from the 64-byte block, as GET SINGLE would return it."""
    d = SETTINGS_DEFS[key]
    return int.from_bytes(raw_64b[d["ram_off"]:d["ram_off"] + d["size"]], "big")


def parse_settings_block(raw_64b: bytes) -> dict:
    """Parse 64-byte settings block from RAM 0x881CB720 into a human-readable dict."""
    if len(raw_64b) < 64:
        raise ValueError(f"Settings block must be at least 64 bytes, got {len(raw_64b)}")

    v = {d["name"]: setting_value(raw_64b, k) for k, d in SETTINGS_DEFS.items()}
    cal_x_min, cal_x_max, cal_y_min, cal_y_max = struct.unpack(">4i", raw_64b[0x0C:0x1C])
    return {
        "raw_block": raw_64b[:64].hex(),
        "cut_speed": v["cut_speed"],
        "cut_pressure": v["cut_pressure"],
        "cut_pressure_relative": v["cut_pressure"] - 10,
        "draw_speed": v["draw_speed"],
        "draw_pressure": v["draw_pressure"],
        "draw_pressure_relative": v["draw_pressure"] - 10,
        "auto_off": bool(v["auto_off"]),
        "auto_off_hours": v["auto_off_hours"],
        "auto_off_base_sec": v["auto_off_base_sec"],
        "sound": bool(v["sound"]),
        "unit": "inch" if v["unit"] else "mm",
        "unit_raw": v["unit"],
        "layout_mode": v["layout_mode"],
        "language_id": v["language"],
        "language_name": LANGUAGE_NAMES.get(v["language"], f"Unknown ({v['language']})"),
        "level_0x13": struct.unpack(">I", raw_64b[0x00:0x04])[0],
        "flag_0x21": raw_64b[0x06],                                   # EEPROM 0x21, not in 0xC5
        "mode_0x19": struct.unpack(">I", raw_64b[0x2C:0x30])[0],      # EEPROM 0x19, not in 0xC5
        "calibration": {
            "x_min": cal_x_min,
            "x_max": cal_x_max,
            "y_min": cal_y_min,
            "y_max": cal_y_max,
        }
    }


def _asm_settings(syms=SETTINGS_SYMBOLS):
    """MIPS assembly routine for settings get/set handler (CDB[0] == 0xC5)."""
    def L(x):
        return f"lui $t9, {(x>>16)&0xffff}\n    ori $t9, $t9, {x&0xffff}"

    f_check = syms['scsi_check']
    f_send = syms['scsi_send']
    f_complete = syms['scsi_complete']
    f_eeprom_write = syms['eeprom_write']
    ram_base = syms['settings_ram']
    scsi_buf = syms['scsi_buf']

    return f"""
    # -------------------------------------------------------------
    # Opcode 0xC5: SETTINGS Gateway
    # a0 = CDB ptr
    # -------------------------------------------------------------
settings_dispatch:
    addiu $sp, $sp, -48
    sw    $ra, 44($sp)
    sw    $s0, 40($sp)
    sw    $s1, 36($sp)
    sw    $s2, 32($sp)
    sw    $s3, 28($sp)
    sw    $s4, 24($sp)
    sw    $s5, 20($sp)
    sw    $s6, 16($sp)

    move  $s0, $a0        # s0 = CDB
    {L(ram_base)}
    move  $s3, $t9        # s3 = 0x881CB720 (settings RAM base)

    lbu   $s1, 1($s0)     # s1 = flag (CDB[1]: 0=GET, 1=SET+EEPROM, 2=SET RAM-only)
    lbu   $t0, 2($s0)     # t0 = setting_key (CDB[2])

    # If flag == 0 and setting_key == 0: GET ALL SETTINGS
    bnez  $s1, decode_key
    beqz  $t0, get_all_settings

decode_key:
    # Match t0 against known setting keys:
    # Key 0x01 or 0x14: Cut Speed
    ori   $t4, $zero, 0x01
    beq   $t0, $t4, match_cut_speed
    ori   $t4, $zero, 0x14
    beq   $t0, $t4, match_cut_speed

    # Key 0x02 or 0x15: Cut Pressure
    ori   $t4, $zero, 0x02
    beq   $t0, $t4, match_cut_pressure
    ori   $t4, $zero, 0x15
    beq   $t0, $t4, match_cut_pressure

    # Key 0x03 or 0x16: Draw Speed
    ori   $t4, $zero, 0x03
    beq   $t0, $t4, match_draw_speed
    ori   $t4, $zero, 0x16
    beq   $t0, $t4, match_draw_speed

    # Key 0x04 or 0x17: Draw Pressure
    ori   $t4, $zero, 0x04
    beq   $t0, $t4, match_draw_pressure
    ori   $t4, $zero, 0x17
    beq   $t0, $t4, match_draw_pressure

    # Key 0x05 or 0x18: Auto power-off hours
    ori   $t4, $zero, 0x05
    beq   $t0, $t4, match_auto_shutdown
    ori   $t4, $zero, 0x18
    beq   $t0, $t4, match_auto_shutdown

    # Key 0x06 or 0x22: Auto power-off on/off
    ori   $t4, $zero, 0x06
    beq   $t0, $t4, match_buzzer
    ori   $t4, $zero, 0x22
    beq   $t0, $t4, match_buzzer

    # Key 0x07 or 0x10: Unit (mm / inch)
    ori   $t4, $zero, 0x07
    beq   $t0, $t4, match_unit
    ori   $t4, $zero, 0x10
    beq   $t0, $t4, match_unit

    # Key 0x08 or 0x11: Layout mode
    ori   $t4, $zero, 0x08
    beq   $t0, $t4, match_pattern_interval
    ori   $t4, $zero, 0x11
    beq   $t0, $t4, match_pattern_interval

    # Key 0x09 or 0x23: Language
    ori   $t4, $zero, 0x09
    beq   $t0, $t4, match_language
    ori   $t4, $zero, 0x23
    beq   $t0, $t4, match_language

    # Key 0x0A or 0x13: level 0-9 (low half of the +0x00 word)
    ori   $t4, $zero, 0x0A
    beq   $t0, $t4, match_seam_allowance
    ori   $t4, $zero, 0x13
    beq   $t0, $t4, match_seam_allowance

    # Key 0x0B or 0x12: Key beep on/off
    ori   $t4, $zero, 0x0B
    beq   $t0, $t4, match_cut_area
    ori   $t4, $zero, 0x12
    beq   $t0, $t4, match_cut_area

    # Key 0x0C or 0x24: Auto power-off base seconds
    ori   $t4, $zero, 0x0C
    beq   $t0, $t4, match_auto_sec
    ori   $t4, $zero, 0x24
    beq   $t0, $t4, match_auto_sec

key_not_found:
    # If key unknown: in GET mode return 0xFFFFFFFF, in SET mode no-op complete
    bnez  $s1, set_finish
    {L(scsi_buf)}
    nor   $t0, $zero, $zero
    sw    $t0, 0($t9)
    move  $s2, $t9
    {L(f_check)}
    ori   $a0, $zero, 1
    ori   $a1, $zero, 4
    jalr  $t9
    {L(f_send)}
    move  $a0, $s2
    ori   $a1, $zero, 0x1400
    ori   $a2, $zero, 4
    ori   $a3, $zero, 4
    jalr  $t9
    j     settings_exit

match_cut_speed:
    ori   $s4, $zero, 0x1C  # ram_off = 0x1C
    ori   $s5, $zero, 1     # size = 1
    ori   $s6, $zero, 0x14  # eeprom = 0x14
    j     perform_action

match_cut_pressure:
    ori   $s4, $zero, 0x1D
    ori   $s5, $zero, 1
    ori   $s6, $zero, 0x15
    j     perform_action

match_draw_speed:
    ori   $s4, $zero, 0x1E
    ori   $s5, $zero, 1
    ori   $s6, $zero, 0x16
    j     perform_action

match_draw_pressure:
    ori   $s4, $zero, 0x1F
    ori   $s5, $zero, 1
    ori   $s6, $zero, 0x17
    j     perform_action

match_auto_shutdown:
    ori   $s4, $zero, 0x20
    ori   $s5, $zero, 1
    ori   $s6, $zero, 0x18
    j     perform_action

match_buzzer:
    ori   $s4, $zero, 0x28  # 4-byte word at 0x28
    ori   $s5, $zero, 4
    ori   $s6, $zero, 0x22
    j     perform_action

match_unit:
    ori   $s4, $zero, 0x04
    ori   $s5, $zero, 1
    ori   $s6, $zero, 0x10
    j     perform_action

match_pattern_interval:
    ori   $s4, $zero, 0x08  # 4-byte word at 0x08
    ori   $s5, $zero, 4
    ori   $s6, $zero, 0x11
    j     perform_action

match_language:
    ori   $s4, $zero, 0x30
    ori   $s5, $zero, 1
    ori   $s6, $zero, 0x23
    j     perform_action

match_seam_allowance:
    ori   $s4, $zero, 0x02  # 2-byte halfword at 0x02
    ori   $s5, $zero, 2
    ori   $s6, $zero, 0x13
    j     perform_action

match_cut_area:
    ori   $s4, $zero, 0x05
    ori   $s5, $zero, 1
    ori   $s6, $zero, 0x12
    j     perform_action

match_auto_sec:
    ori   $s4, $zero, 0x24  # 4-byte word at 0x24
    ori   $s5, $zero, 4
    ori   $s6, $zero, 0x24

perform_action:
    # If s1 != 0, jump to set_action
    bnez  $s1, set_action

    # -------------------------------------------------------------
    # GET SINGLE: Read value from s3 + s4
    # -------------------------------------------------------------
get_single:
    addu  $t1, $s3, $s4
    ori   $t2, $zero, 1
    beq   $s5, $t2, get_read_u8
    ori   $t2, $zero, 2
    beq   $s5, $t2, get_read_u16

get_read_u32:
    lw    $v0, 0($t1)
    j     get_stage_resp

get_read_u16:
    lhu   $v0, 0($t1)
    j     get_stage_resp

get_read_u8:
    lbu   $v0, 0($t1)

get_stage_resp:
    # Stage 32-bit big-endian value at scsi_buf (0x8820f820)
    {L(scsi_buf)}
    sw    $v0, 0($t9)
    move  $s2, $t9

    {L(f_check)}
    ori   $a0, $zero, 1   # data-IN
    ori   $a1, $zero, 4   # 4 bytes
    jalr  $t9

    {L(f_send)}
    move  $a0, $s2        # src = scsi_buf
    ori   $a1, $zero, 0x1400
    ori   $a2, $zero, 4
    ori   $a3, $zero, 4
    jalr  $t9

    j     settings_exit

    # -------------------------------------------------------------
    # GET ALL: Return entire 64-byte block from 0x881CB720
    # -------------------------------------------------------------
get_all_settings:
    {L(f_check)}
    ori   $a0, $zero, 1   # dir = 1 (data-IN)
    ori   $a1, $zero, 64  # len = 64
    jalr  $t9

    {L(f_send)}
    move  $a0, $s3        # src = 0x881CB720
    ori   $a1, $zero, 0x1400
    ori   $a2, $zero, 64
    ori   $a3, $zero, 64
    jalr  $t9

    j     settings_exit

    # -------------------------------------------------------------
    # SET ACTION: Unpack value from CDB[4:8] -> store to RAM -> commit EEPROM
    # -------------------------------------------------------------
set_action:
    # Unpack 32-bit big-endian value from CDB[4:8] into s2
    lbu   $s2, 4($s0)
    sll   $s2, $s2, 8
    lbu   $t1, 5($s0)
    or    $s2, $s2, $t1
    sll   $s2, $s2, 8
    lbu   $t1, 6($s0)
    or    $s2, $s2, $t1
    sll   $s2, $s2, 8
    lbu   $t1, 7($s0)
    or    $s2, $s2, $t1   # s2 = 32-bit value

    # Store into RAM at s3 + s4
    addu  $t1, $s3, $s4
    ori   $t2, $zero, 1
    beq   $s5, $t2, set_store_u8
    ori   $t2, $zero, 2
    beq   $s5, $t2, set_store_u16

set_store_u32:
    sw    $s2, 0($t1)
    j     set_check_buzzer

set_store_u16:
    sh    $s2, 0($t1)
    j     set_check_eeprom

set_store_u8:
    sb    $s2, 0($t1)

set_check_buzzer:
    # Key 0x06 (eeprom 0x22) also mirrors 0/1 into +0x07 (0x881CB727). No stock code reads
    # that byte (it is padding), so this is harmless.
    ori   $t3, $zero, 0x22
    bne   $s6, $t3, set_check_eeprom
    sltu  $t4, $zero, $s2 # 1 if nonzero, 0 if zero
    sb    $t4, 7($s3)

set_check_eeprom:
    # Check if EEPROM commit requested (flag == 1)
    ori   $t1, $zero, 1
    bne   $s1, $t1, set_finish

    # Commit to EEPROM: WriteByteBoth(a0=eeprom_addr, a1=val, a2=0)
    {L(f_eeprom_write)}
    move  $a0, $s6        # EEPROM address
    andi  $a1, $s2, 0xff  # value byte
    move  $a2, $zero      # 1 byte
    jalr  $t9

set_finish:
    # No-data command completion
    {L(f_check)}
    move  $a0, $zero
    move  $a1, $zero
    jalr  $t9

    {L(f_complete)}
    jalr  $t9

settings_exit:
    lw    $ra, 44($sp)
    lw    $s0, 40($sp)
    lw    $s1, 36($sp)
    lw    $s2, 32($sp)
    lw    $s3, 28($sp)
    lw    $s4, 24($sp)
    lw    $s5, 20($sp)
    lw    $s6, 16($sp)
    addiu $sp, $sp, 48
    jr    $ra
"""
