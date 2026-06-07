#!/usr/bin/env python3
"""
mx800_capture.py — Acquisition complète Philips IntelliVue MX800
Remplace VSCaptureMP : numériques + démographiques → SQLite + CSV

Usage :
  python3 mx800_capture.py --ip 192.168.100.31
  python3 mx800_capture.py --ip 192.168.100.31 --db /home/hegp/hegp.db --csv /home/hegp/data/

Référence : Philips Interface Programming Guide (PIPG) 4535 642 59271
"""

import socket
import struct
import sqlite3
import csv
import json
import os
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

try:
    import h5py
    import numpy as np
    HDF5_AVAILABLE = True
except ImportError:
    HDF5_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger('mx800')

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTANTES PROTOCOLE (PIPG)
# ═══════════════════════════════════════════════════════════════════════════════

ROIV_APDU  = 0x0001
RORS_APDU  = 0x0002
ROLRS_APDU = 0x0005

CMD_CONFIRMED_EVENT_REPORT = 0x0001
CMD_CONFIRMED_ACTION       = 0x0007

NOM_NOTI_MDS_CREAT   = 0x0D06
NOM_ACT_POLL_MDIB_DATA = 0x0C16

NOM_MOC_VMS_MDS          = 0x0021
NOM_MOC_VMO_METRIC_NU    = 0x0006
NOM_MOC_PT_DEMOG         = 0x002A

NOM_ATTR_GRP_METRIC_VAL_OBS = 0x0803
NOM_ATTR_GRP_PT_DEMOG       = 0x0000  # 0x0000 = tous groupes → retourne tout
NOM_ATTR_GRP_VMO_STATIC     = 0x0811  # pour récupérer scale/sample_period

# Waveforms
NOM_MOC_VMO_METRIC_SA_RT   = 0x0009
NOM_ACT_POLL_MDIB_DATA_EXT = 0xF13B
NOM_ATTR_SA_VAL_OBS        = 0x096E
NOM_ATTR_SA_CMPD_VAL_OBS   = 0x0967
NOM_ATTR_SCALE_SPECN_I16   = 0x096F
NOM_ATTR_TIME_PD_SAMP      = 0x098D

# Identifiants physiologiques des waveforms (PIPG p.179-188)
WAVE_MAP = {
    0x0101: 'ECG_I',
    0x0102: 'ECG_II',
    0x013D: 'ECG_III',
    0x013E: 'ECG_aVR',
    0x013F: 'ECG_aVL',
    0x0140: 'ECG_aVF',
    0x0143: 'ECG_V',
    0x4BB4: 'Pleth',
    0x4A14: 'ABP_wave',
    0x4A10: 'ART_wave',
    0x4A44: 'CVP_wave',
    0x4A1C: 'PAP_wave',
    0x5000: 'Resp',
    0x50AC: 'CO2_wave',
}

NOM_ATTR_NU_VAL_OBS      = 0x0950
NOM_ATTR_NU_CMPD_VAL_OBS = 0x094B
NOM_ATTR_PT_DEMOG_ST     = 0x0957
NOM_ATTR_PT_NAME_GIVEN   = 0x095D
NOM_ATTR_PT_NAME_FAMILY  = 0x095C
NOM_ATTR_PT_ID           = 0x095A
NOM_ATTR_PT_SEX          = 0x0961
NOM_ATTR_PT_TYPE         = 0x0962
NOM_ATTR_PT_DOB          = 0x0958
NOM_ATTR_PT_AGE          = 0x09D8
NOM_ATTR_PT_HEIGHT       = 0x09DC
NOM_ATTR_PT_WEIGHT       = 0x09DF

PAT_DEMO_STATE = {0: 'EMPTY', 1: 'PRE_ADMITTED', 2: 'ADMITTED', 8: 'DISCHARGED'}
PAT_SEX        = {0: 'UNKNOWN', 1: 'MALE', 2: 'FEMALE', 9: 'UNSPECIFIED'}
PAT_TYPE       = {0: 'UNSPECIFIED', 1: 'ADULT', 2: 'PEDIATRIC', 3: 'NEONATAL'}

# ── Identifiants physiologiques (PIPG p.115-180) ─────────────────────────────
# Clé = physio_id (OIDType), Valeur = (nom_court, unité)
PHYSIO_MAP = {
    # Cardio-vasculaire
    0x4182: ('HR',          'bpm'),
    0x4BB8: ('SpO2',        '%'),
    0x4822: ('Pulse',       'bpm'),
    # PA invasive ABP
    0x4A15: ('ABP_sys',     'mmHg'),
    0x4A16: ('ABP_dia',     'mmHg'),
    0x4A17: ('ABP_mean',    'mmHg'),
    0x4A14: ('ABP',         'mmHg'),
    # PA ART (générique)
    0x4A11: ('ART_sys',     'mmHg'),
    0x4A12: ('ART_dia',     'mmHg'),
    0x4A13: ('ART_mean',    'mmHg'),
    0x4A10: ('ART',         'mmHg'),
    # Aorte
    0x4A0D: ('Ao_sys',      'mmHg'),
    0x4A0E: ('Ao_dia',      'mmHg'),
    0x4A0F: ('Ao_mean',     'mmHg'),
    # PAP
    0x4A1D: ('PAP_sys',     'mmHg'),
    0x4A1E: ('PAP_dia',     'mmHg'),
    0x4A1F: ('PAP_mean',    'mmHg'),
    # CVP
    0x4A44: ('CVP',         'mmHg'),
    0x4A47: ('CVP_mean',    'mmHg'),
    # Débit cardiaque
    0x4B04: ('CO',          'L/min'),
    0x4BDC: ('CCO',         'L/min'),
    0x490C: ('CI',          'L/min/m2'),
    0xF047: ('CCI',         'L/min/m2'),
    0x4B84: ('SV',          'mL'),
    0xF048: ('SI',          'mL/m2'),
    0xF049: ('SVV',         '%'),
    # Saturation O2
    0x4B34: ('SaO2',        '%'),
    0x4B3C: ('SvO2',        '%'),
    0xF100: ('ScvO2',       '%'),
    # Températures
    0x4B48: ('Temp',        '°C'),
    0xE004: ('Trect',       '°C'),
    0xE014: ('Tblood',      '°C'),
    0x4B60: ('Tcore',       '°C'),
    0x4B74: ('Tskin',       '°C'),
    0x4B64: ('Tesoph',      '°C'),
    0x4B6C: ('Tnaso',       '°C'),
    0x4B50: ('Tart',        '°C'),
    0xF0C7: ('T1',          '°C'),
    0xF0C8: ('T2',          '°C'),
    # CO2 / Respiratoire
    0x50AC: ('CO2',         'mmHg'),
    0x50B0: ('EtCO2',       'mmHg'),
    0x50BA: ('FiCO2',       'mmHg'),
    0x5012: ('RR',          'rpm'),
    # NBP (non invasive)
    0x4A05: ('NBP_sys',     'mmHg'),
    0x4A06: ('NBP_dia',     'mmHg'),
    0x4A07: ('NBP_mean',    'mmHg'),
}

# Ports
MX800_DATA_PORT = 24105
LOCAL_PORT      = 24106

# ═══════════════════════════════════════════════════════════════════════════════
# ASSOCIATION REQUEST (bytes PIPG p.298-305)
# ═══════════════════════════════════════════════════════════════════════════════

ASSOC_REQ_SESSION_HEADER = bytes([0x0D])
ASSOC_REQ_SESSION_DATA   = bytes([
    0x05, 0x08, 0x13, 0x01, 0x00, 0x16, 0x01, 0x02,
    0x80, 0x00, 0x14, 0x02, 0x00, 0x02
])
ASSOC_REQ_PRES_HEADER = bytes([
    0xC1, 0x00, 0x31, 0x80, 0xA0, 0x80, 0x80, 0x01,
    0x01, 0x00, 0x00, 0xA2, 0x80, 0xA0, 0x03, 0x00,
    0x00, 0x01, 0xA4, 0x80, 0x30, 0x80, 0x02, 0x01,
    0x01, 0x06, 0x04, 0x52, 0x01, 0x00, 0x01, 0x30,
    0x80, 0x06, 0x02, 0x51, 0x01, 0x00, 0x00, 0x00,
    0x00, 0x30, 0x80, 0x02, 0x01, 0x02, 0x06, 0x0C,
    0x2A, 0x86, 0x48, 0xCE, 0x14, 0x02, 0x01, 0x00,
    0x00, 0x00, 0x01, 0x01, 0x30, 0x80, 0x06, 0x0C,
    0x2A, 0x86, 0x48, 0xCE, 0x14, 0x02, 0x01, 0x00,
    0x00, 0x00, 0x02, 0x01, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x61, 0x80, 0x30, 0x80, 0x02, 0x01,
    0x01, 0xA0, 0x80, 0x60, 0x80, 0xA1, 0x80, 0x06,
    0x0C, 0x2A, 0x86, 0x48, 0xCE, 0x14, 0x02, 0x01,
    0x00, 0x00, 0x00, 0x03, 0x01, 0x00, 0x00, 0xBE,
    0x80, 0x28, 0x80, 0x06, 0x0C, 0x2A, 0x86, 0x48,
    0xCE, 0x14, 0x02, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x01, 0x02, 0x01, 0x02, 0x81
])
ASSOC_REQ_USER_DATA = bytes([
    0x48,
    0x80, 0x00, 0x00, 0x00,
    0x40, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x80, 0x00, 0x00, 0x00,
    0x20, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x01, 0x00, 0x2C,
    0x00, 0x01, 0x00, 0x28,
    0x80, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x09, 0xC4,
    0x00, 0x00, 0x03, 0xE8,
    0x00, 0x00, 0x03, 0xE8,
    0xFF, 0xFF, 0xFF, 0xFF,
    0x60, 0x00, 0x00, 0x00,
    0x00, 0x01, 0x00, 0x0C,
    0xF0, 0x01, 0x00, 0x08,
    0x80, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
])

# Association Request avec waveforms (POLL_EXT_PERIOD_NU_1SEC | POLL_EXT_PERIOD_RTSA)
ASSOC_REQ_USER_DATA_WAVES = bytes([
    0x48,
    0x80, 0x00, 0x00, 0x00,
    0x40, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x80, 0x00, 0x00, 0x00,
    0x20, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x01, 0x00, 0x2C,
    0x00, 0x01, 0x00, 0x28,
    0x80, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x09, 0xC4,
    0x00, 0x00, 0x03, 0xE8,
    0x00, 0x00, 0x05, 0xB0,  # max_mtu_rx=1456 pour les waves
    0xFF, 0xFF, 0xFF, 0xFF,
    0x60, 0x00, 0x00, 0x00,
    0x00, 0x01, 0x00, 0x0C,
    0xF0, 0x01, 0x00, 0x08,
    0xA0, 0x00, 0x00, 0x00,  # POLL_EXT_PERIOD_NU_1SEC(0x80) | POLL_EXT_PERIOD_RTSA(0x20)
    0x00, 0x00, 0x00, 0x00,
])
ASSOC_REQ_PRES_TRAILER = bytes(16)

RELEASE_REQ = bytes([
    0x09, 0x18,
    0xC1, 0x16, 0x61, 0x80, 0x30, 0x80,
    0x02, 0x01, 0x01, 0xA0, 0x80, 0x62,
    0x80, 0x80, 0x01, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])

# ═══════════════════════════════════════════════════════════════════════════════
# CONSTRUCTION MESSAGES
# ═══════════════════════════════════════════════════════════════════════════════

def build_spdu(payload: bytes) -> bytes:
    return struct.pack('>HH', 0xE100, 0x0002) + payload

def build_roiv(invoke_id: int, cmd_type: int, payload: bytes) -> bytes:
    roiv_inner = struct.pack('>HHH', invoke_id, cmd_type, len(payload)) + payload
    return struct.pack('>HH', ROIV_APDU, len(roiv_inner)) + roiv_inner

def build_action(invoke_id: int, managed_obj_class: int, action_type: int, payload: bytes) -> bytes:
    action_data = (
        struct.pack('>HHH', managed_obj_class, 0, 0) +
        struct.pack('>I', 0) +
        struct.pack('>H', action_type) +
        struct.pack('>H', len(payload)) +
        payload
    )
    return build_spdu(build_roiv(invoke_id, CMD_CONFIRMED_ACTION, action_data))

def build_poll(invoke_id: int, obj_class: int, attr_grp: int) -> bytes:
    payload = struct.pack('>HHHH', invoke_id & 0xFFFF, 0x0001, obj_class, attr_grp)
    return build_action(invoke_id & 0xFFFF, NOM_MOC_VMS_MDS, NOM_ACT_POLL_MDIB_DATA, payload)

def build_assoc_request(waves: bool = False) -> bytes:
    user_data = ASSOC_REQ_USER_DATA_WAVES if waves else ASSOC_REQ_USER_DATA
    inner = ASSOC_REQ_PRES_HEADER[2:] + user_data + ASSOC_REQ_PRES_TRAILER
    pres = bytes([ASSOC_REQ_PRES_HEADER[0], len(inner)]) + inner
    session_body = ASSOC_REQ_SESSION_DATA + pres
    return bytes([ASSOC_REQ_SESSION_HEADER[0], len(session_body)]) + session_body

def build_extended_poll(invoke_id: int, obj_class: int, attr_grp: int,
                        period_ms: int = 256) -> bytes:
    """Extended Poll Data Request pour les waveforms (PIPG p.59)."""
    # PollDataReqPeriod: active_period en 1/8ms ticks
    active_ticks = period_ms * 8 * 8  # 256ms × 8 × 8 = 16384 ticks = 2 secondes actives
    # poll_ext_attr: NOM_ATTR_TIME_PD_POLL + PollDataReqPeriod
    ext_attr = struct.pack('>HHHI',
        0xF13E,          # NOM_ATTR_TIME_PD_POLL
        4,               # length
        active_ticks >> 16, active_ticks & 0xFFFF  # RelativeTime (u32)
    )
    # En fait RelativeTime est u32 → repack
    ext_attr = struct.pack('>HHI', 0xF13E, 4, active_ticks)
    attr_list = struct.pack('>HH', 1, len(ext_attr)) + ext_attr

    payload = struct.pack('>HHHH', invoke_id & 0xFFFF, 0x0001, obj_class, attr_grp)
    payload += attr_list

    action_data = (
        struct.pack('>HHH', NOM_MOC_VMS_MDS, 0, 0) +
        struct.pack('>I', 0) +
        struct.pack('>H', NOM_ACT_POLL_MDIB_DATA_EXT) +
        struct.pack('>H', len(payload)) +
        payload
    )
    return build_spdu(build_roiv(invoke_id & 0xFFFF, CMD_CONFIRMED_ACTION, action_data))

def build_mds_create_result(invoke_id: int, managed_obj: bytes, event_time: bytes) -> bytes:
    evt_result = managed_obj + event_time + struct.pack('>HH', NOM_NOTI_MDS_CREAT, 0)
    rors_inner = struct.pack('>HHH', invoke_id, CMD_CONFIRMED_EVENT_REPORT, len(evt_result)) + evt_result
    rors = struct.pack('>HH', RORS_APDU, len(rors_inner)) + rors_inner
    return build_spdu(rors)

# ═══════════════════════════════════════════════════════════════════════════════
# PARSING FLOAT (format Philips FLOAT-Type)
# ═══════════════════════════════════════════════════════════════════════════════

def parse_float_type(raw: int) -> float | None:
    """
    Philips FLOAT-Type : mantisse 24 bits signée + exposant 8 bits signé.
    Valeurs spéciales : 0x007FFFFF=NaN, 0x00800000=NRes, 0x00400000=+Inf, etc.
    """
    SPECIAL = {0x007FFFFF, 0x00800000, 0x00400000, 0x00C00000, 0x00800001}
    if raw in SPECIAL:
        return None
    exp = raw >> 24
    if exp & 0x80:
        exp = exp - 256  # signé
    mantissa = raw & 0x00FFFFFF
    if mantissa & 0x00800000:
        mantissa = mantissa - 0x01000000  # signé 24 bits
    return mantissa * (10 ** exp)

# ═══════════════════════════════════════════════════════════════════════════════
# PARSING MESSAGES
# ═══════════════════════════════════════════════════════════════════════════════

def detect_message_type(data: bytes) -> str:
    """Retourne le type de message reçu."""
    if not data:
        return 'UNKNOWN'
    b0 = data[0]
    if b0 == 0x0E:
        return 'ASSOC_RESPONSE'
    if b0 == 0x0C:
        return 'REFUSE'
    if b0 == 0x19:
        return 'ABORT'
    if b0 == 0x0A:
        return 'RELEASE_RESPONSE'
    if len(data) >= 12:
        # SPpdu (4) + ROapdus ro_type (2)
        ro_type = struct.unpack_from('>H', data, 4)[0]
        if ro_type == ROIV_APDU and len(data) >= 14:
            cmd_type = struct.unpack_from('>H', data, 10)[0]
            if cmd_type == CMD_CONFIRMED_EVENT_REPORT:
                # check event_type
                if len(data) >= 26:
                    event_type = struct.unpack_from('>H', data, 24)[0]
                    if event_type == NOM_NOTI_MDS_CREAT:
                        return 'MDS_CREATE'
        if ro_type in (RORS_APDU, ROLRS_APDU):
            return 'POLL_RESULT'
    return 'UNKNOWN'

def parse_mds_create(data: bytes):
    """Retourne (invoke_id, managed_obj_bytes, event_time_bytes) ou None."""
    try:
        offset = 8   # SPpdu(4) + ROapdus(4)
        invoke_id = struct.unpack_from('>H', data, offset)[0]
        offset += 6  # invoke_id(2) + cmd_type(2) + length(2)
        managed_obj = data[offset:offset + 6]
        event_time  = data[offset + 6:offset + 10]
        return invoke_id, managed_obj, event_time
    except Exception:
        return None

def parse_string_attr(data: bytes) -> str:
    if len(data) < 2:
        return ''
    str_len = struct.unpack_from('>H', data)[0]
    if str_len == 0 or 2 + str_len > len(data):
        return ''
    raw = data[2:2 + str_len]
    # Philips encode les strings en UTF-16 big-endian
    try:
        return raw.decode('utf-16-be').rstrip('\x00').strip()
    except Exception:
        return raw.rstrip(b'\x00').decode('utf-8', errors='replace').strip()

def parse_poll_payload(data: bytes) -> tuple[bytes, int] | tuple[None, None]:
    """
    Extrait (PollMdibDataReply, invoke_id) depuis un paquet brut.
    Gère RORS (résultat final) et ROLRS (linked result intermédiaire).
    """
    try:
        offset = 4  # SPpdu
        ro_type = struct.unpack_from('>H', data, offset)[0]
        offset += 4  # ROapdus

        if ro_type == RORS_APDU:
            invoke_id = struct.unpack_from('>H', data, offset)[0]
            offset += 6  # invoke_id(2)+cmd(2)+len(2)
        elif ro_type == ROLRS_APDU:
            invoke_id = struct.unpack_from('>H', data, offset + 2)[0]
            offset += 8  # state(1)+count(1)+invoke_id(2)+cmd(2)+len(2)
        else:
            return None, None

        offset += 10  # ActionResult: managed_obj(6)+action_type(2)+length(2)
        return data[offset:], invoke_id
    except Exception:
        return None, None

def parse_attr_list(data: bytes, offset: int) -> tuple[list, int]:
    """
    Parse une AttributeList à partir de offset.
    Retourne ([(attr_id, val_bytes), ...], nouvel_offset)
    """
    attrs = []
    if offset + 4 > len(data):
        return attrs, offset
    count  = struct.unpack_from('>H', data, offset)[0]
    length = struct.unpack_from('>H', data, offset + 2)[0]
    offset += 4
    end = offset + length
    for _ in range(count):
        if offset + 4 > len(data):
            break
        attr_id = struct.unpack_from('>H', data, offset)[0]
        val_len = struct.unpack_from('>H', data, offset + 2)[0]
        offset += 4
        val_data = data[offset:offset + val_len]
        offset += val_len
        attrs.append((attr_id, val_data))
    return attrs, end

def parse_nu_obs_value(val_data: bytes) -> dict:
    """Parse NuObsValue : physio_id(2)+state(2)+unit(2)+float(4)"""
    result = {}
    if len(val_data) < 10:
        return result
    physio_id = struct.unpack_from('>H', val_data, 0)[0]
    state     = struct.unpack_from('>H', val_data, 2)[0]
    # Valide si octet haut de state == 0
    if state & 0x8000:  # seulement INVALID bloque la valeur
        return result
    raw_float = struct.unpack_from('>I', val_data, 6)[0]
    value = parse_float_type(raw_float)
    if value is None:
        return result
    name, unit = PHYSIO_MAP.get(physio_id, (f'0x{physio_id:04X}', ''))
    result[name] = round(value, 4)
    return result

def parse_poll_result(payload: bytes, obj_class: int) -> dict:
    """
    Parse PollMdibDataReply pour extraire les valeurs.
    Retourne un dict {nom: valeur}.
    """
    result = {}
    try:
        # poll_number(2)+rel_time(4)+abs_time(8)+TYPE(4)+attr_grp(2)
        offset = 20
        if offset + 4 > len(payload):
            return result

        # PollInfoList: count(2)+length(2)
        poll_count = struct.unpack_from('>H', payload, offset)[0]
        offset += 4
        if poll_count == 0:
            return result

        # Itère sur SingleContextPoll
        for _ in range(poll_count):
            if offset + 6 > len(payload):
                break
            offset += 2  # context_id
            obs_count  = struct.unpack_from('>H', payload, offset)[0]
            obs_length = struct.unpack_from('>H', payload, offset + 2)[0]
            offset += 4
            obs_end = offset + obs_length

            for _ in range(obs_count):
                if offset + 4 > len(payload):
                    break
                offset += 2  # obj_handle
                attrs, offset = parse_attr_list(payload, offset)

                for attr_id, val_data in attrs:
                    # ── Numériques ──────────────────────────────────────────
                    if attr_id == NOM_ATTR_NU_VAL_OBS:
                        result.update(parse_nu_obs_value(val_data))

                    elif attr_id == NOM_ATTR_NU_CMPD_VAL_OBS:
                        if len(val_data) >= 4:
                            nu_count = struct.unpack_from('>H', val_data)[0]
                            nu_off = 4
                            for _ in range(nu_count):
                                if nu_off + 10 > len(val_data):
                                    break
                                result.update(parse_nu_obs_value(val_data[nu_off:nu_off + 10]))
                                nu_off += 10

                    # ── Démographiques ──────────────────────────────────────
                    elif attr_id == NOM_ATTR_PT_DEMOG_ST and len(val_data) >= 2:
                        s = struct.unpack_from('>H', val_data)[0]
                        result['demo_state'] = PAT_DEMO_STATE.get(s, f'0x{s:04X}')

                    elif attr_id == NOM_ATTR_PT_NAME_GIVEN:
                        result['given_name'] = parse_string_attr(val_data)

                    elif attr_id == NOM_ATTR_PT_NAME_FAMILY:
                        result['family_name'] = parse_string_attr(val_data)

                    elif attr_id == NOM_ATTR_PT_ID:
                        result['patient_id'] = parse_string_attr(val_data)

                    elif attr_id == NOM_ATTR_PT_SEX and len(val_data) >= 2:
                        s = struct.unpack_from('>H', val_data)[0]
                        result['sex'] = PAT_SEX.get(s, '')

                    elif attr_id == NOM_ATTR_PT_TYPE and len(val_data) >= 2:
                        t = struct.unpack_from('>H', val_data)[0]
                        result['patient_type'] = PAT_TYPE.get(t, '')

            offset = obs_end

    except Exception as e:
        log.debug(f"Erreur parse_poll_result: {e}")

    return result

# ═══════════════════════════════════════════════════════════════════════════════
# BASE DE DONNÉES SQLITE
# ═══════════════════════════════════════════════════════════════════════════════

def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            monitor_ip  TEXT,
            start_time  TEXT,
            end_time    TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER,
            patient_id  TEXT,
            family_name TEXT,
            given_name  TEXT,
            sex         TEXT,
            patient_type TEXT,
            demo_state  TEXT,
            admitted_at TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS numerics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER,
            patient_db_id INTEGER,
            timestamp   TEXT,
            HR          REAL, SpO2       REAL, Pulse      REAL,
            ABP_sys     REAL, ABP_dia    REAL, ABP_mean   REAL,
            ART_sys     REAL, ART_dia    REAL, ART_mean   REAL,
            Ao_sys      REAL, Ao_dia     REAL, Ao_mean    REAL,
            PAP_sys     REAL, PAP_dia    REAL, PAP_mean   REAL,
            CVP         REAL, CVP_mean   REAL,
            NBP_sys     REAL, NBP_dia    REAL, NBP_mean   REAL,
            CO          REAL, CCO        REAL, CI         REAL,
            CCI         REAL, SV         REAL, SI         REAL, SVV REAL,
            SaO2        REAL, SvO2       REAL, ScvO2      REAL,
            Temp        REAL, Trect      REAL, Tblood     REAL,
            Tcore       REAL, Tskin      REAL, Tesoph     REAL,
            Tnaso       REAL, Tart       REAL, T1         REAL, T2 REAL,
            EtCO2       REAL, FiCO2      REAL, RR         REAL,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_numerics_ts
        ON numerics(session_id, timestamp)
    """)
    conn.commit()
    return conn

# Colonnes numériques pour INSERT dynamique
NUMERIC_COLS = [
    'HR', 'SpO2', 'Pulse',
    'ABP_sys', 'ABP_dia', 'ABP_mean',
    'ART_sys', 'ART_dia', 'ART_mean',
    'Ao_sys',  'Ao_dia',  'Ao_mean',
    'PAP_sys', 'PAP_dia', 'PAP_mean',
    'CVP', 'CVP_mean',
    'NBP_sys', 'NBP_dia', 'NBP_mean',
    'CO', 'CCO', 'CI', 'CCI', 'SV', 'SI', 'SVV',
    'SaO2', 'SvO2', 'ScvO2',
    'Temp', 'Trect', 'Tblood', 'Tcore', 'Tskin', 'Tesoph', 'Tnaso', 'Tart', 'T1', 'T2',
    'EtCO2', 'FiCO2', 'RR',
]

def insert_numerics(conn, session_id, patient_db_id, ts, values: dict):
    row = {col: values.get(col) for col in NUMERIC_COLS}
    cols = ['session_id', 'patient_db_id', 'timestamp'] + NUMERIC_COLS
    placeholders = ','.join(['?'] * len(cols))
    vals = [session_id, patient_db_id, ts] + [row[c] for c in NUMERIC_COLS]
    conn.execute(
        f"INSERT INTO numerics ({','.join(cols)}) VALUES ({placeholders})",
        vals
    )
    conn.commit()

def upsert_patient(conn, session_id, demo: dict) -> int:
    """Insère ou met à jour le patient, retourne son id DB."""
    cur = conn.execute(
        "SELECT id FROM patients WHERE session_id=? AND patient_id=?",
        (session_id, demo.get('patient_id', ''))
    )
    row = cur.fetchone()
    if row:
        conn.execute("""
            UPDATE patients SET family_name=?, given_name=?, sex=?,
            patient_type=?, demo_state=? WHERE id=?
        """, (
            demo.get('family_name', ''), demo.get('given_name', ''),
            demo.get('sex', ''), demo.get('patient_type', ''),
            demo.get('demo_state', ''), row[0]
        ))
        conn.commit()
        return row[0]
    else:
        cur = conn.execute("""
            INSERT INTO patients
            (session_id, patient_id, family_name, given_name, sex, patient_type, demo_state, admitted_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            session_id,
            demo.get('patient_id', ''),
            demo.get('family_name', ''),
            demo.get('given_name', ''),
            demo.get('sex', ''),
            demo.get('patient_type', ''),
            demo.get('demo_state', ''),
            datetime.now().isoformat()
        ))
        conn.commit()
        return cur.lastrowid

# ═══════════════════════════════════════════════════════════════════════════════
# CSV
# ═══════════════════════════════════════════════════════════════════════════════

CSV_COLS = ['timestamp', 'patient_id', 'family_name', 'given_name'] + NUMERIC_COLS

def get_csv_writer(csv_dir: str, session_id: int, patient_id: str):
    """Retourne (file_handle, csv_writer) pour la session en cours."""
    Path(csv_dir).mkdir(parents=True, exist_ok=True)
    date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = f"session_{session_id}_{date_str}_{patient_id or 'unknown'}.csv"
    fpath = os.path.join(csv_dir, fname)
    f = open(fpath, 'w', newline='', encoding='utf-8')
    writer = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction='ignore')
    writer.writeheader()
    log.info(f"CSV ouvert : {fpath}")
    return f, writer

# ═══════════════════════════════════════════════════════════════════════════════
# PARSING WAVEFORMS
# ═══════════════════════════════════════════════════════════════════════════════

def parse_sa_obs_value(val_data: bytes) -> tuple[int, list[float]] | None:
    """
    Parse SaObsValue : physio_id(2) + state(2) + length(2) + samples(u16[])
    Retourne (physio_id, [samples bruts]) ou None si invalide.
    """
    if len(val_data) < 6:
        return None
    physio_id = struct.unpack_from('>H', val_data, 0)[0]
    state     = struct.unpack_from('>H', val_data, 2)[0]
    if state & 0x8000:
        return None
    arr_len = struct.unpack_from('>H', val_data, 4)[0]
    if arr_len == 0 or 6 + arr_len > len(val_data):
        return None
    raw_bytes = val_data[6:6 + arr_len]
    # Échantillons en u16 big-endian
    n = arr_len // 2
    samples = list(struct.unpack_from(f'>{n}H', raw_bytes))
    return physio_id, samples

def parse_wave_poll_result(payload: bytes) -> dict:
    """
    Parse PollMdibDataReply pour les waveforms.
    Retourne {nom_canal: [samples_bruts], ...}
    """
    result = {}
    try:
        offset = 20  # poll_num(2)+rel_time(4)+abs_time(8)+TYPE(4)+attr_grp(2)
        if offset + 4 > len(payload):
            return result

        poll_count = struct.unpack_from('>H', payload, offset)[0]
        offset += 4
        if poll_count == 0:
            return result

        for _ in range(poll_count):
            if offset + 6 > len(payload):
                break
            offset += 2  # context_id
            obs_count  = struct.unpack_from('>H', payload, offset)[0]
            obs_length = struct.unpack_from('>H', payload, offset + 2)[0]
            offset += 4
            obs_end = offset + obs_length

            for _ in range(obs_count):
                if offset + 4 > len(payload):
                    break
                offset += 2  # obj_handle
                attrs, offset = parse_attr_list(payload, offset)

                for attr_id, val_data in attrs:
                    if attr_id == NOM_ATTR_SA_VAL_OBS:
                        parsed = parse_sa_obs_value(val_data)
                        if parsed:
                            physio_id, samples = parsed
                            name = WAVE_MAP.get(physio_id, f'wave_0x{physio_id:04X}')
                            result[name] = samples

                    elif attr_id == NOM_ATTR_SA_CMPD_VAL_OBS:
                        if len(val_data) >= 4:
                            sa_count = struct.unpack_from('>H', val_data)[0]
                            sa_off   = 4
                            for _ in range(sa_count):
                                parsed = parse_sa_obs_value(val_data[sa_off:])
                                if parsed:
                                    physio_id, samples = parsed
                                    name = WAVE_MAP.get(physio_id, f'wave_0x{physio_id:04X}')
                                    result[name] = samples
                                    sa_off += 6 + len(samples) * 2

            offset = obs_end

    except Exception as e:
        log.debug(f"Erreur parse_wave_poll_result: {e}")

    return result

# ═══════════════════════════════════════════════════════════════════════════════
# HDF5
# ═══════════════════════════════════════════════════════════════════════════════

class HDF5Writer:
    """Écrit les waveforms dans un fichier HDF5 par session."""

    def __init__(self, hdf5_dir: str, session_id: int, patient_id: str = 'unknown'):
        if not HDF5_AVAILABLE:
            raise RuntimeError("h5py non installé — lance : pip install h5py numpy --break-system-packages")
        Path(hdf5_dir).mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime('%Y%m%d_%H%M%S')
        fname = f"session_{session_id}_{date_str}_{patient_id}.h5"
        self.path = os.path.join(hdf5_dir, fname)
        self.f = h5py.File(self.path, 'w')
        self.f.attrs['session_id']  = session_id
        self.f.attrs['patient_id']  = patient_id
        self.f.attrs['created_at']  = datetime.now().isoformat()
        self.f.attrs['monitor_protocol'] = 'Philips IntelliVue Data Export UDP'
        # Groupes
        self.waves_grp = self.f.create_group('waves')
        self.meta_grp  = self.f.create_group('patient')
        self.ts_grp    = self.f.create_group('timestamps')
        # Buffers en mémoire (flush toutes les N trames)
        self._buffers  = {}   # canal → [samples]
        self._ts_buf   = {}   # canal → [timestamps]
        self._flush_n  = 100  # flush toutes les 100 trames (~25s à 256ms)
        self._counts   = {}
        log.info(f"HDF5 ouvert : {self.path}")

    def write_patient(self, demo: dict):
        for k, v in demo.items():
            self.meta_grp.attrs[k] = str(v)

    def write_waves(self, ts: str, waves: dict):
        for canal, samples in waves.items():
            if canal not in self._buffers:
                self._buffers[canal] = []
                self._ts_buf[canal]  = []
                self._counts[canal]  = 0

            self._buffers[canal].extend(samples)
            self._ts_buf[canal].append(ts)
            self._counts[canal] += 1

            if self._counts[canal] >= self._flush_n:
                self._flush_canal(canal)

    def _flush_canal(self, canal: str):
        if not self._buffers.get(canal):
            return
        arr = np.array(self._buffers[canal], dtype=np.uint16)
        if canal in self.waves_grp:
            ds = self.waves_grp[canal]
            old_len = ds.shape[0]
            ds.resize(old_len + len(arr), axis=0)
            ds[old_len:] = arr
        else:
            self.waves_grp.create_dataset(
                canal, data=arr,
                maxshape=(None,), chunks=True,
                compression='gzip', compression_opts=4
            )
        # Timestamps
        ts_arr = np.array(self._ts_buf[canal], dtype=h5py.string_dtype())
        if canal in self.ts_grp:
            ds = self.ts_grp[canal]
            old_len = ds.shape[0]
            ds.resize(old_len + len(ts_arr), axis=0)
            ds[old_len:] = ts_arr
        else:
            self.ts_grp.create_dataset(
                canal, data=ts_arr,
                maxshape=(None,), chunks=True
            )
        self._buffers[canal].clear()
        self._ts_buf[canal].clear()
        self._counts[canal] = 0

    def flush_all(self):
        for canal in list(self._buffers.keys()):
            self._flush_canal(canal)
        self.f.flush()

    def close(self):
        self.flush_all()
        self.f.close()
        log.info(f"HDF5 fermé : {self.path}")


# ═══════════════════════════════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════

def run(monitor_ip: str, db_path: str, csv_dir: str, demo_json: str,
        poll_interval: float = 1.0, demo_interval: int = 30,
        waves: bool = False, hdf5_dir: str = '/home/hegp/waves/'):

    if waves and not HDF5_AVAILABLE:
        log.error("--waves nécessite h5py : pip install h5py numpy --break-system-packages")
        waves = False

    conn = init_db(db_path)
    log.info(f"Base SQLite : {db_path}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)
    try:
        sock.bind(('', LOCAL_PORT))
    except OSError:
        sock.bind(('', 0))
    log.info(f"Socket : {sock.getsockname()} → {monitor_ip}:{MX800_DATA_PORT}")

    # État
    associated    = False
    session_id    = None
    patient_db_id = None
    patient_info  = {}
    csv_file      = None
    csv_writer    = None
    hdf5_writer   = None
    poll_num      = 1
    last_nu_poll  = 0.0
    last_demo_poll = 0.0
    last_wave_poll = 0.0
    # Accumulation des linked results (plusieurs paquets pour un même poll)
    pending       = {}   # invoke_id → dict valeurs accumulées
    pending_ts    = {}   # invoke_id → timestamp premier paquet
    pending_obj   = {}   # invoke_id → obj_code

    def send_assoc():
        nonlocal associated
        log.info(f"Envoi Association Request {'(avec waveforms)' if waves else ''}...")
        sock.sendto(build_assoc_request(waves), (monitor_ip, MX800_DATA_PORT))
        associated = False

    def open_session():
        nonlocal session_id
        cur = conn.execute(
            "INSERT INTO sessions (monitor_ip, start_time) VALUES (?,?)",
            (monitor_ip, datetime.now().isoformat())
        )
        conn.commit()
        session_id = cur.lastrowid
        log.info(f"Session DB ouverte : id={session_id}")

    def close_session():
        nonlocal session_id, csv_file, hdf5_writer
        if session_id:
            conn.execute(
                "UPDATE sessions SET end_time=? WHERE id=?",
                (datetime.now().isoformat(), session_id)
            )
            conn.commit()
        if csv_file:
            csv_file.close()
            csv_file = None
        if hdf5_writer:
            hdf5_writer.close()
            hdf5_writer = None

    send_assoc()

    try:
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                mtype = detect_message_type(data)
                log.debug(f"Reçu {len(data)}o [{mtype}] de {addr}")

                # ── Association Response ──────────────────────────────────
                if mtype == 'ASSOC_RESPONSE':
                    log.info("Association établie.")
                    associated = True
                    open_session()
                    last_nu_poll = 0.0
                    last_demo_poll = 0.0

                # ── MDS Create Event ──────────────────────────────────────
                elif mtype == 'MDS_CREATE':
                    parsed = parse_mds_create(data)
                    if parsed:
                        invoke_id, managed_obj, event_time = parsed
                        sock.sendto(
                            build_mds_create_result(invoke_id, managed_obj, event_time),
                            addr
                        )
                        log.info(f"MDS Create Event confirmé (invoke_id={invoke_id})")
                        associated = True

                # ── Refuse ────────────────────────────────────────────────
                elif mtype == 'REFUSE':
                    log.warning("Association refusée. Nouvelle tentative dans 15s...")
                    time.sleep(15)
                    send_assoc()

                # ── Abort ─────────────────────────────────────────────────
                elif mtype == 'ABORT':
                    log.warning("Abort reçu. Reconnexion dans 10s...")
                    associated = False
                    close_session()
                    session_id = None
                    time.sleep(10)
                    send_assoc()

                # ── Poll Result ───────────────────────────────────────────
                elif mtype == 'POLL_RESULT' and associated and session_id:
                    payload, invoke_id = parse_poll_payload(data)
                    if payload is None:
                        continue

                    obj_code = struct.unpack_from('>H', payload, 16)[0] if len(payload) >= 18 else 0
                    values   = parse_poll_result(payload, obj_code)

                    # ── Numériques : accumule les linked results ──────────
                    if obj_code == NOM_MOC_VMO_METRIC_NU:
                        if invoke_id not in pending:
                            pending[invoke_id]     = {}
                            pending_ts[invoke_id]  = datetime.now().isoformat()
                            pending_obj[invoke_id] = obj_code
                        pending[invoke_id].update(values)

                        # Paquet final = RORS ou payload vide (48o terminateur)
                        ro_type  = struct.unpack_from('>H', data, 4)[0]
                        is_final = (ro_type == RORS_APDU) or (len(payload) <= 24)
                        if is_final:
                            merged = pending.pop(invoke_id, {})
                            ts     = pending_ts.pop(invoke_id, datetime.now().isoformat())
                            pending_obj.pop(invoke_id, None)
                            if merged:
                                merged.update({
                                    'patient_id':  patient_info.get('patient_id', ''),
                                    'family_name': patient_info.get('family_name', ''),
                                    'given_name':  patient_info.get('given_name', ''),
                                })
                                insert_numerics(conn, session_id, patient_db_id, ts, merged)
                                if csv_writer:
                                    csv_writer.writerow({'timestamp': ts, **merged})
                                    csv_file.flush()
                                hr  = merged.get('HR', '-')
                                spo = merged.get('SpO2', '-')
                                abp = f"{merged.get('ABP_sys','-')}/{merged.get('ABP_dia','-')}"
                                tmp = merged.get('Tblood') or merged.get('Tcore') or merged.get('Temp', '-')
                                log.info(f"HR={hr} SpO2={spo}% ABP={abp} mmHg T={tmp}°C")

                    # ── Démographiques ────────────────────────────────────
                    elif obj_code == NOM_MOC_PT_DEMOG:
                        demo = values
                        if demo and demo.get('demo_state') == 'ADMITTED':
                            changed = demo.get('patient_id') != patient_info.get('patient_id')
                            patient_info  = demo
                            patient_db_id = upsert_patient(conn, session_id, demo)
                            if changed or csv_writer is None:
                                if csv_file:
                                    csv_file.close()
                                csv_file, csv_writer = get_csv_writer(
                                    csv_dir, session_id, demo.get('patient_id', 'unknown')
                                )
                            if waves and (changed or hdf5_writer is None):
                                if hdf5_writer:
                                    hdf5_writer.close()
                                hdf5_writer = HDF5Writer(
                                    hdf5_dir, session_id, demo.get('patient_id', 'unknown')
                                )
                                hdf5_writer.write_patient(demo)
                            with open(demo_json, 'w') as f:
                                json.dump({**demo, 'timestamp': datetime.now().isoformat()},
                                          f, ensure_ascii=False, indent=2)
                            log.info(f"Patient : {demo.get('family_name')} {demo.get('given_name')} "
                                     f"ID={demo.get('patient_id')}")

                    # ── Waveforms ─────────────────────────────────────────
                    elif obj_code == NOM_MOC_VMO_METRIC_SA_RT:
                        if waves and hdf5_writer:
                            wave_values = parse_wave_poll_result(payload)
                            if wave_values:
                                ts = datetime.now().isoformat()
                                hdf5_writer.write_waves(ts, wave_values)
                                canaux = list(wave_values.keys())
                                log.debug(f"Waves reçues : {canaux}")


            except socket.timeout:
                pass
            except Exception as e:
                log.error(f"Erreur réception : {e}")

            if not associated:
                continue

            now = time.time()

            # ── Poll numériques (toutes les poll_interval secondes) ───────
            if now - last_nu_poll >= poll_interval:
                sock.sendto(
                    build_poll(poll_num, NOM_MOC_VMO_METRIC_NU, NOM_ATTR_GRP_METRIC_VAL_OBS),
                    (monitor_ip, MX800_DATA_PORT)
                )
                poll_num = (poll_num + 1) & 0xFFFF
                last_nu_poll = now

            # ── Poll démographiques (toutes les demo_interval secondes) ───
            if now - last_demo_poll >= demo_interval:
                sock.sendto(
                    build_poll(poll_num, NOM_MOC_PT_DEMOG, NOM_ATTR_GRP_PT_DEMOG),
                    (monitor_ip, MX800_DATA_PORT)
                )
                poll_num = (poll_num + 1) & 0xFFFF
                last_demo_poll = now

            # ── Poll waveforms (toutes les 256ms si --waves) ──────────────
            if waves and (now - last_wave_poll >= 0.256):
                sock.sendto(
                    build_extended_poll(poll_num, NOM_MOC_VMO_METRIC_SA_RT,
                                        NOM_ATTR_GRP_METRIC_VAL_OBS, 256),
                    (monitor_ip, MX800_DATA_PORT)
                )
                poll_num = (poll_num + 1) & 0xFFFF
                last_wave_poll = now

    except KeyboardInterrupt:
        log.info("Arrêt demandé...")
    finally:
        sock.sendto(RELEASE_REQ, (monitor_ip, MX800_DATA_PORT))
        close_session()
        sock.close()
        conn.close()
        log.info("Terminé proprement.")


# ═══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Acquisition MX800 complète')
    parser.add_argument('--ip',      default='192.168.100.31',
                        help='IP du moniteur (défaut: 192.168.100.31)')
    parser.add_argument('--db',      default='/home/hegp/hegp.db',
                        help='Chemin base SQLite (défaut: /home/hegp/hegp.db)')
    parser.add_argument('--csv',     default='/home/hegp/data/',
                        help='Dossier CSV (défaut: /home/hegp/data/)')
    parser.add_argument('--json',    default='/home/hegp/patient_demo.json',
                        help='Fichier JSON démographiques pour Flask')
    parser.add_argument('--interval', type=float, default=1.0,
                        help='Intervalle poll numériques en secondes (défaut: 1.0)')
    parser.add_argument('--demo-interval', type=int, default=30,
                        help='Intervalle poll démographiques en secondes (défaut: 30)')
    parser.add_argument('--waves',   action='store_true',
                        help='Activer la capture des waveforms (ECG, ABP, Pleth...) → HDF5')
    parser.add_argument('--hdf5',    default='/home/hegp/waves/',
                        help='Dossier HDF5 pour les waveforms (défaut: /home/hegp/waves/)')
    parser.add_argument('--debug',   action='store_true')
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    run(
        monitor_ip    = args.ip,
        db_path       = args.db,
        csv_dir       = args.csv,
        demo_json     = args.json,
        poll_interval = args.interval,
        demo_interval = args.demo_interval,
        waves         = args.waves,
        hdf5_dir      = args.hdf5,
    )
