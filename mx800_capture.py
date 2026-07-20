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
import ipaddress
import logging
from datetime import datetime
from pathlib import Path

try:
    import h5py
    import numpy as np
    HDF5_AVAILABLE = True
except ImportError:
    HDF5_AVAILABLE = False

# ═══════════════════════════════════════════════════════════════════════════════
# CHARGEMENT CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG_PATH = '/home/hegp/config.json'

def load_config(config_path: str = DEFAULT_CONFIG_PATH) -> dict:
    """
    Charge la configuration depuis config.json.
    Construit PHYSIO_MAP et NUMERIC_COLS dynamiquement.
    Retourne le dict de config complet.
    """
    global PHYSIO_MAP, NUMERIC_COLS_DYNAMIC

    if not os.path.exists(config_path):
        log.warning(f"config.json introuvable ({config_path}) — utilisation des valeurs par défaut")
        return {}

    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    params = cfg.get('parameters', {})
    PHYSIO_MAP.clear()
    NUMERIC_COLS_DYNAMIC.clear()

    for key, val in params.items():
        if key.startswith('_'):
            continue  # commentaires de section
        if not val.get('active', True):
            continue  # paramètre désactivé
        try:
            physio_id = int(key, 16)
            name = val['name']
            unit = val.get('unit', '')
            PHYSIO_MAP[physio_id] = (name, unit)
            if name not in NUMERIC_COLS_DYNAMIC:
                NUMERIC_COLS_DYNAMIC.append(name)
        except (ValueError, KeyError) as e:
            log.warning(f"Paramètre ignoré ({key}): {e}")

    log.info(f"Config chargée : {len(PHYSIO_MAP)} paramètres actifs")
    log.debug(f"Paramètres : {[v[0] for v in PHYSIO_MAP.values()]}")
    return cfg

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

# PHYSIO_MAP est chargé dynamiquement depuis config.json (voir load_config())
# Ne pas modifier ici — éditer config.json à la place
PHYSIO_MAP = {}   # rempli au démarrage par load_config()
NUMERIC_COLS_DYNAMIC = []  # rempli au démarrage par load_config()

# Ports
MX800_DATA_PORT = 24105
LOCAL_PORT      = 24106

# Découverte automatique du moniteur
DISCOVERY_ADDR  = '255.255.255.255'  # broadcast limité (atteint le segment L2 local)
ASSOC_RETRY_SEC = 4                  # ré-émission de l'Association Request tant que non associé

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
    # Une « intervention » regroupe TOUTES les données d'un même patient en
    # cumulé, à travers les déconnexions/reconnexions réseau. Une nouvelle
    # intervention démarre dès que le patient_id change (donc un patient qui
    # revient après un autre patient = une nouvelle intervention). Une
    # intervention peut couvrir plusieurs sessions ; start_session_id note
    # seulement la session où elle a débuté.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS interventions (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            start_session_id INTEGER,
            patient_id       TEXT,
            family_name      TEXT,
            given_name       TEXT,
            sex              TEXT,
            patient_type     TEXT,
            start_time       TEXT,
            end_time         TEXT,
            FOREIGN KEY(start_session_id) REFERENCES sessions(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER,
            intervention_id INTEGER,
            patient_id  TEXT,
            family_name TEXT,
            given_name  TEXT,
            sex         TEXT,
            patient_type TEXT,
            demo_state  TEXT,
            admitted_at TEXT,
            FOREIGN KEY(session_id) REFERENCES sessions(id),
            FOREIGN KEY(intervention_id) REFERENCES interventions(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS numerics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  INTEGER,
            intervention_id INTEGER,
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
            FOREIGN KEY(session_id) REFERENCES sessions(id),
            FOREIGN KEY(intervention_id) REFERENCES interventions(id)
        )
    """)
    # Migration des bases antérieures : garantit la colonne intervention_id
    # en INTEGER AVANT de créer l'index qui la référence (sinon la migration
    # générique de insert_numerics l'ajouterait en REAL et stockerait les id
    # d'intervention en flottant).
    for tbl in ('numerics', 'patients'):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})")}
        if 'intervention_id' not in cols:
            log.info(f"Migration DB : ajout colonne 'intervention_id' à {tbl}")
            conn.execute(f"ALTER TABLE {tbl} ADD COLUMN intervention_id INTEGER")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_numerics_ts
        ON numerics(session_id, timestamp)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_numerics_intervention
        ON numerics(intervention_id, timestamp)
    """)
    conn.commit()
    return conn

_COLS_BASE = ['timestamp', 'patient_id', 'family_name', 'given_name']

def get_numeric_cols():
    """Retourne les colonnes numériques actives (depuis config.json)."""
    # NUMERIC_COLS_DYNAMIC vaut [] tant que load_config() n'a rien chargé ;
    # on renvoie alors une liste vide plutôt qu'une variable inexistante.
    return NUMERIC_COLS_DYNAMIC

def insert_numerics(conn, session_id, intervention_id, patient_db_id, ts, values: dict):
    cols = get_numeric_cols()
    row  = {col: values.get(col) for col in cols}
    all_cols = ['session_id', 'intervention_id', 'patient_db_id', 'timestamp'] + cols
    placeholders = ','.join(['?'] * len(all_cols))
    vals = [session_id, intervention_id, patient_db_id, ts] + [row[c] for c in cols]
    # Add any columns not yet in the schema before inserting
    existing = {row[1] for row in conn.execute("PRAGMA table_info(numerics)")}
    for col in all_cols:
        if col not in existing:
            log.info(f"Migration DB : ajout colonne '{col}'")
            conn.execute(f"ALTER TABLE numerics ADD COLUMN {col} REAL")
    conn.commit()
    conn.execute(
        f"INSERT INTO numerics ({','.join(all_cols)}) VALUES ({placeholders})",
        vals
    )
    conn.commit()

def open_intervention(conn, start_session_id, demo: dict) -> int:
    """
    Ouvre une nouvelle intervention pour le patient courant et retourne son id.
    Appelée au premier patient identifié et à chaque changement de patient_id.
    """
    cur = conn.execute("""
        INSERT INTO interventions
        (start_session_id, patient_id, family_name, given_name, sex, patient_type, start_time)
        VALUES (?,?,?,?,?,?,?)
    """, (
        start_session_id,
        demo.get('patient_id', ''),
        demo.get('family_name', ''),
        demo.get('given_name', ''),
        demo.get('sex', ''),
        demo.get('patient_type', ''),
        datetime.now().isoformat()
    ))
    conn.commit()
    iid = cur.lastrowid
    log.info(f"Nouvelle intervention : id={iid} "
             f"patient={demo.get('patient_id') or 'unknown'} "
             f"({demo.get('family_name','')} {demo.get('given_name','')})")
    return iid

def close_intervention(conn, intervention_id):
    """Horodate la fin d'une intervention (changement de patient ou arrêt)."""
    if intervention_id:
        conn.execute(
            "UPDATE interventions SET end_time=? WHERE id=?",
            (datetime.now().isoformat(), intervention_id)
        )
        conn.commit()

def upsert_patient(conn, intervention_id, session_id, demo: dict) -> int:
    """
    Insère ou met à jour le patient de l'intervention en cours, retourne son id DB.
    Une intervention = un patient : la ligne patients est unique par intervention.
    """
    cur = conn.execute(
        "SELECT id FROM patients WHERE intervention_id=?",
        (intervention_id,)
    )
    row = cur.fetchone()
    if row:
        conn.execute("""
            UPDATE patients SET session_id=?, family_name=?, given_name=?, sex=?,
            patient_type=?, demo_state=? WHERE id=?
        """, (
            session_id,
            demo.get('family_name', ''), demo.get('given_name', ''),
            demo.get('sex', ''), demo.get('patient_type', ''),
            demo.get('demo_state', ''), row[0]
        ))
        conn.commit()
        return row[0]
    else:
        cur = conn.execute("""
            INSERT INTO patients
            (session_id, intervention_id, patient_id, family_name, given_name, sex, patient_type, demo_state, admitted_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (
            session_id,
            intervention_id,
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

CSV_COLS = _COLS_BASE  # colonnes de base — complétées dynamiquement au runtime

def get_csv_writer(csv_dir: str, intervention_id: int, patient_id: str):
    """
    Retourne (file_handle, csv_writer) pour l'intervention en cours.
    Le fichier est nommé par intervention et ouvert en append : les données
    d'un même patient restent cumulées dans un seul CSV, même après une
    reconnexion réseau. L'en-tête n'est écrit que si le fichier est neuf.
    """
    Path(csv_dir).mkdir(parents=True, exist_ok=True)
    fname = f"intervention_{intervention_id}_{patient_id or 'unknown'}.csv"
    fpath = os.path.join(csv_dir, fname)
    is_new = (not os.path.exists(fpath)) or os.path.getsize(fpath) == 0
    f = open(fpath, 'a', newline='', encoding='utf-8')
    fieldnames = _COLS_BASE + get_numeric_cols()
    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
    if is_new:
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

    def __init__(self, hdf5_dir: str, intervention_id: int, patient_id: str = 'unknown'):
        if not HDF5_AVAILABLE:
            raise RuntimeError("h5py non installé — lance : pip install h5py numpy --break-system-packages")
        Path(hdf5_dir).mkdir(parents=True, exist_ok=True)
        # Nommage par intervention + mode 'a' : les waveforms d'un même patient
        # restent cumulées dans un seul fichier, même après une reconnexion.
        fname = f"intervention_{intervention_id}_{patient_id}.h5"
        self.path = os.path.join(hdf5_dir, fname)
        self.f = h5py.File(self.path, 'a')
        self.f.attrs['intervention_id'] = intervention_id
        self.f.attrs['patient_id']  = patient_id
        if 'created_at' not in self.f.attrs:
            self.f.attrs['created_at'] = datetime.now().isoformat()
        self.f.attrs['monitor_protocol'] = 'Philips IntelliVue Data Export UDP'
        # Groupes (require_group : réutilise ceux déjà présents en mode append)
        self.waves_grp = self.f.require_group('waves')
        self.meta_grp  = self.f.require_group('patient')
        self.ts_grp    = self.f.require_group('timestamps')
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
        waves: bool = False, hdf5_dir: str = '/home/hegp/waves/',
        config_path: str = DEFAULT_CONFIG_PATH, discovery_cidr: str = ''):

    # Charge la config (PHYSIO_MAP + NUMERIC_COLS_DYNAMIC)
    cfg = load_config(config_path)
    # Les args CLI ont priorité sur config.json
    if cfg:
        monitor_ip     = monitor_ip     or cfg.get('monitor_ip',     monitor_ip)
        db_path        = db_path        or cfg.get('db_path',        db_path)
        csv_dir        = csv_dir        or cfg.get('csv_dir',        csv_dir)
        demo_json      = demo_json      or cfg.get('demo_json',      demo_json)
        poll_interval  = poll_interval  or cfg.get('poll_interval',  poll_interval)
        demo_interval  = demo_interval  or cfg.get('demo_interval',  demo_interval)
        waves          = waves          or cfg.get('waves',          waves)
        hdf5_dir       = hdf5_dir       or cfg.get('hdf5_dir',       hdf5_dir)
        discovery_cidr = discovery_cidr or cfg.get('discovery_cidr', discovery_cidr)

    if waves and not HDF5_AVAILABLE:
        log.error("--waves nécessite h5py : pip install h5py numpy --break-system-packages")
        waves = False

    conn = init_db(db_path)
    log.info(f"Base SQLite : {db_path}")

    # Découverte automatique si monitor_ip vaut "auto" ou est vide : on ne connaît
    # pas encore l'IP du moniteur, on l'apprend du premier paquet qu'il renvoie.
    auto_discover = (not monitor_ip) or str(monitor_ip).strip().lower() == 'auto'
    if auto_discover:
        monitor_ip = None
    # Repli optionnel pour réseaux routés/VLAN : balayage d'une plage CIDR.
    discovery_hosts = []
    if auto_discover and discovery_cidr:
        try:
            net = ipaddress.ip_network(str(discovery_cidr), strict=False)
            discovery_hosts = [str(h) for h in net.hosts()]
        except ValueError as e:
            log.warning(f"discovery_cidr invalide ({discovery_cidr}) : {e}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)
    if auto_discover:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(('', LOCAL_PORT))
    except OSError:
        sock.bind(('', 0))
    if auto_discover:
        scan = f" + scan {len(discovery_hosts)} IP" if discovery_hosts else ""
        log.info(f"Socket : {sock.getsockname()} → découverte automatique du moniteur "
                 f"(broadcast{scan})")
    else:
        log.info(f"Socket : {sock.getsockname()} → {monitor_ip}:{MX800_DATA_PORT}")

    # État
    associated    = False
    session_id    = None
    intervention_id    = None   # épisode patient courant (cumulé)
    current_patient_id = None   # patient_id de l'intervention en cours
    patient_db_id = None
    patient_info  = {}
    csv_file      = None
    csv_writer    = None
    hdf5_writer   = None
    poll_num      = 1
    last_nu_poll  = 0.0
    last_demo_poll = 0.0
    last_wave_poll = 0.0
    last_assoc_send = 0.0
    # Accumulation des linked results (plusieurs paquets pour un même poll)
    pending       = {}   # invoke_id → dict valeurs accumulées
    pending_ts    = {}   # invoke_id → timestamp premier paquet
    pending_obj   = {}   # invoke_id → obj_code

    def send_assoc():
        nonlocal associated, last_assoc_send
        if monitor_ip:
            sock.sendto(build_assoc_request(waves), (monitor_ip, MX800_DATA_PORT))
            log.info(f"Envoi Association Request → {monitor_ip} "
                     f"{'(avec waveforms)' if waves else ''}")
        else:
            # Découverte : broadcast sur le segment (+ balayage CIDR optionnel)
            pkt = build_assoc_request(waves)
            for target in (DISCOVERY_ADDR, *discovery_hosts):
                try:
                    sock.sendto(pkt, (target, MX800_DATA_PORT))
                except OSError:
                    pass
            scan = f" + scan {len(discovery_hosts)} IP" if discovery_hosts else ""
            log.info(f"Recherche du moniteur (broadcast{scan})...")
        associated = False
        last_assoc_send = time.time()

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
        nonlocal session_id, csv_file, csv_writer, hdf5_writer
        if session_id:
            conn.execute(
                "UPDATE sessions SET end_time=? WHERE id=?",
                (datetime.now().isoformat(), session_id)
            )
            conn.commit()
        if csv_file:
            csv_file.close()
        csv_file   = None
        csv_writer = None
        if hdf5_writer:
            hdf5_writer.close()
            hdf5_writer = None
        session_id = None

    send_assoc()

    try:
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                mtype = detect_message_type(data)
                log.debug(f"Reçu {len(data)}o [{mtype}] de {addr}")

                # ── Découverte : adopte l'IP du 1er moniteur qui répond ───
                # Verrouillé une fois associé : si plusieurs moniteurs répondent
                # sur le segment, on ne bascule pas de l'un à l'autre.
                if auto_discover and not associated and monitor_ip != addr[0] \
                        and mtype in ('ASSOC_RESPONSE', 'MDS_CREATE'):
                    monitor_ip = addr[0]
                    log.info(f"Moniteur découvert à l'adresse {monitor_ip}")

                # ── Association Response ──────────────────────────────────
                # Ignore les réponses en double (retransmissions, multi-NIC) :
                # on n'ouvre pas de nouvelle session tant qu'on est déjà associé.
                if mtype == 'ASSOC_RESPONSE' and not associated:
                    log.info("Association établie.")
                    associated = True
                    open_session()
                    last_nu_poll = 0.0
                    last_demo_poll = 0.0

                # ── MDS Create Event ──────────────────────────────────────
                # Toujours confirmer : le moniteur ré-émet cet événement et coupe
                # l'association (ABORT) au bout de ~10 s s'il ne reçoit pas la
                # confirmation — y compris quand une Association Response a déjà
                # été reçue juste avant (sinon on saute la confirmation → ABORT).
                if mtype == 'MDS_CREATE':
                    parsed = parse_mds_create(data)
                    if parsed:
                        invoke_id, managed_obj, event_time = parsed
                        sock.sendto(
                            build_mds_create_result(invoke_id, managed_obj, event_time),
                            addr
                        )
                        if not associated:
                            log.info(f"MDS Create Event confirmé (invoke_id={invoke_id})")
                            associated = True
                            if session_id is None:
                                open_session()
                            last_nu_poll = 0.0
                            last_demo_poll = 0.0

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
                    if auto_discover:
                        monitor_ip = None   # ré-apprend l'IP au prochain send_assoc
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
                            if merged and session_id:
                                merged.update({
                                    'patient_id':  patient_info.get('patient_id', ''),
                                    'family_name': patient_info.get('family_name', ''),
                                    'given_name':  patient_info.get('given_name', ''),
                                })
                                insert_numerics(conn, session_id, intervention_id, patient_db_id, ts, merged)
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
                            # Nouvelle intervention si aucune en cours, ou si le
                            # patient_id diffère de celui de l'intervention
                            # courante. Un même patient (même après reconnexion)
                            # reste dans la même intervention ; un patient qui
                            # revient après un autre patient = nouvelle intervention.
                            new_patient = demo.get('patient_id')
                            changed = (intervention_id is None) or (new_patient != current_patient_id)
                            patient_info = demo
                            if changed:
                                close_intervention(conn, intervention_id)
                                intervention_id    = open_intervention(conn, session_id, demo)
                                current_patient_id = new_patient
                            patient_db_id = upsert_patient(conn, intervention_id, session_id, demo)
                            if changed or csv_writer is None:
                                if csv_file:
                                    csv_file.close()
                                csv_file   = None
                                csv_writer = None
                                csv_file, csv_writer = get_csv_writer(
                                    csv_dir, intervention_id, demo.get('patient_id', 'unknown')
                                )
                            if waves and (changed or hdf5_writer is None):
                                if hdf5_writer:
                                    hdf5_writer.close()
                                hdf5_writer = HDF5Writer(
                                    hdf5_dir, intervention_id, demo.get('patient_id', 'unknown')
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
                # Relance périodiquement l'Association Request (découverte ou
                # simple reconnexion) tant que le moniteur n'a pas répondu.
                if time.time() - last_assoc_send >= ASSOC_RETRY_SEC:
                    send_assoc()
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
        if monitor_ip:
            try:
                sock.sendto(RELEASE_REQ, (monitor_ip, MX800_DATA_PORT))
            except OSError:
                pass
        close_intervention(conn, intervention_id)
        close_session()
        sock.close()
        conn.close()
        log.info("Terminé proprement.")


# ═══════════════════════════════════════════════════════════════════════════════
# POINT D'ENTRÉE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Acquisition MX800 complète')
    parser.add_argument('--config',  default=DEFAULT_CONFIG_PATH,
                        help=f'Fichier de configuration JSON (défaut: {DEFAULT_CONFIG_PATH})')
    parser.add_argument('--ip',      default='',
                        help='IP du moniteur, ou "auto" pour la découverte automatique '
                             '(défaut: depuis config.json ; vide ou "auto" = découverte)')
    parser.add_argument('--discover-cidr', default='',
                        help='Plage CIDR à balayer en découverte si le broadcast ne '
                             'suffit pas (ex. 192.168.1.0/24 ; réseaux routés/VLAN)')
    parser.add_argument('--db',      default='',
                        help='Chemin base SQLite (défaut: depuis config.json)')
    parser.add_argument('--csv',     default='',
                        help='Dossier CSV (défaut: depuis config.json)')
    parser.add_argument('--json',    default='',
                        help='Fichier JSON démographiques pour Flask')
    parser.add_argument('--interval', type=float, default=0,
                        help='Intervalle poll numériques en secondes')
    parser.add_argument('--demo-interval', type=int, default=0,
                        help='Intervalle poll démographiques en secondes')
    parser.add_argument('--waves',   action='store_true',
                        help='Activer la capture des waveforms → HDF5')
    parser.add_argument('--hdf5',    default='',
                        help='Dossier HDF5 pour les waveforms')
    parser.add_argument('--debug',   action='store_true')
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    run(
        monitor_ip     = args.ip,   # vide ou "auto" → découverte automatique
        db_path        = args.db      or '/home/hegp/hegp.db',
        csv_dir        = args.csv     or '/home/hegp/data/',
        demo_json      = args.json    or '/home/hegp/patient_demo.json',
        poll_interval  = args.interval or 1.0,
        demo_interval  = args.demo_interval or 30,
        waves          = args.waves,
        hdf5_dir       = args.hdf5    or '/home/hegp/waves/',
        config_path    = args.config,
        discovery_cidr = args.discover_cidr,
    )
