#!/usr/bin/env python3
"""
demo_poll.py — Polling démographique Philips IntelliVue MX800
Implémente le protocole Data Export (UDP) pour récupérer les données patient.
Référence : Philips Interface Programming Guide (PIPG) 4535 642 59271

Usage : python3 demo_poll.py --ip 192.168.100.31 --out /home/hegp/patient_demo.json
"""

import socket
import struct
import json
import time
import argparse
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('demo_poll')

# ─── Constantes protocole (PIPG) ────────────────────────────────────────────

# Remote Operation types
ROIV_APDU  = 0x0001
RORS_APDU  = 0x0002

# Command types
CMD_CONFIRMED_EVENT_REPORT = 0x0001
CMD_CONFIRMED_ACTION       = 0x0007

# Event types
NOM_NOTI_MDS_CREAT = 0x0D06

# Action types
NOM_ACT_POLL_MDIB_DATA = 0x0C16  # 3094

# Object classes
NOM_MOC_VMS_MDS   = 0x0021  # 33
NOM_MOC_PT_DEMOG  = 0x002A  # 42

# Attribute groups
NOM_ATTR_GRP_PT_DEMOG = 0x0807

# Attribute IDs démographiques
NOM_ATTR_PT_DEMOG_ST    = 0x0957
NOM_ATTR_PT_NAME_GIVEN  = 0x095D
NOM_ATTR_PT_NAME_FAMILY = 0x095C
NOM_ATTR_PT_ID          = 0x095A  # NOM_ATTR_PT_LIFETIME_ID
NOM_ATTR_PT_SEX         = 0x0961
NOM_ATTR_PT_DOB         = 0x0958
NOM_ATTR_PT_AGE         = 0x09D8
NOM_ATTR_PT_HEIGHT      = 0x09DC
NOM_ATTR_PT_WEIGHT      = 0x09DF
NOM_ATTR_PT_TYPE        = 0x0962

# PatDemoState
PAT_DEMO_STATE = {0: 'EMPTY', 1: 'PRE_ADMITTED', 2: 'ADMITTED', 8: 'DISCHARGED'}
PAT_SEX        = {0: 'UNKNOWN', 1: 'MALE', 2: 'FEMALE', 9: 'UNSPECIFIED'}
PAT_TYPE       = {0: 'UNSPECIFIED', 1: 'ADULT', 2: 'PEDIATRIC', 3: 'NEONATAL'}

# Ports
MX800_DATA_PORT = 24105
LOCAL_PORT      = 24106  # port local arbitraire

# ─── Association Request (bytes fixes extraits du PIPG p.298-304) ────────────

# Header session + présentation (fixe sauf longueurs)
ASSOC_REQ_SESSION_HEADER = bytes([0x0D])  # CN_SPDU_SI
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

# UserData = MDSEUserInfoStd (PIPG p.305 — COLD_START, 1min averaged)
ASSOC_REQ_USER_DATA = bytes([
    0x48,                                    # ASNLength = 72
    # MDSEUserInfoStd
    0x80, 0x00, 0x00, 0x00,                  # protocol_version: MDDL_VERSION1
    0x40, 0x00, 0x00, 0x00,                  # nomenclature_version
    0x00, 0x00, 0x00, 0x00,                  # functional_units: 0
    0x80, 0x00, 0x00, 0x00,                  # system_type: SYST_CLIENT
    0x20, 0x00, 0x00, 0x00,                  # startup_mode: COLD_START
    # option_list (count=0, length=0)
    0x00, 0x00, 0x00, 0x00,
    # supported_aprofiles: count=1, length=44
    0x00, 0x01, 0x00, 0x2C,
    # AVAType: NOM_POLL_PROFILE_SUPPORT=0x0001, length=40
    0x00, 0x01, 0x00, 0x28,
    # PollProfileSupport
    0x80, 0x00, 0x00, 0x00,                  # poll_profile_revision: POLL_PROFILE_REV_0
    0x00, 0x00, 0x09, 0xC4,                  # min_poll_period: 2500 (2.5s en 1/8ms)
    0x00, 0x00, 0x03, 0xE8,                  # max_mtu_rx: 1000
    0x00, 0x00, 0x03, 0xE8,                  # max_mtu_tx: 1000
    0xFF, 0xFF, 0xFF, 0xFF,                  # max_bw_tx
    0x60, 0x00, 0x00, 0x00,                  # options: P_OPT_DYN_CREATE|DELETE
    # optional_packages: count=1, length=12
    0x00, 0x01, 0x00, 0x0C,
    # NOM_ATTR_POLL_PROFILE_EXT=0xF001, length=8
    0xF0, 0x01, 0x00, 0x08,
    # PollProfileExt: POLL_EXT_PERIOD_NU_1SEC=0x80000000
    0x80, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,                  # ext_attr: count=0, length=0
])

ASSOC_REQ_PRES_TRAILER = bytes(16)  # 16 x 0x00

RELEASE_REQ = bytes([
    0x09, 0x18,                              # ReleaseReqSessionHeader
    0xC1, 0x16, 0x61, 0x80, 0x30, 0x80,
    0x02, 0x01, 0x01, 0xA0, 0x80, 0x62,
    0x80, 0x80, 0x01, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
])

# ─── Helpers de construction / parsing ──────────────────────────────────────

def build_spdu(payload: bytes) -> bytes:
    """Enveloppe SPpdu : session_id=0xE100, p_context_id=2"""
    return struct.pack('>HH', 0xE100, 0x0002) + payload

def build_roiv(invoke_id: int, cmd_type: int, payload: bytes) -> bytes:
    """ROapdus ROIV + ROIVapdu"""
    roiv_inner = struct.pack('>HHH', invoke_id, cmd_type, len(payload)) + payload
    return struct.pack('>HH', ROIV_APDU, len(roiv_inner)) + roiv_inner

def build_action(invoke_id: int, managed_obj_class: int, action_type: int, payload: bytes) -> bytes:
    """ActionArgument"""
    action_data = struct.pack('>HHHIIH',
        managed_obj_class,  # m_obj_class
        0,                  # context_id
        0,                  # handle
        0,                  # scope
        action_type,        # action_type
        len(payload)        # length
    ) + payload
    return build_spdu(build_roiv(invoke_id, CMD_CONFIRMED_ACTION, action_data))

def build_poll_demog(poll_number: int) -> bytes:
    """SINGLE POLL DATA REQUEST pour Patient Demographics"""
    # PollMdibDataReq: poll_number, TYPE(partition+code), polled_attr_grp
    payload = struct.pack('>HHHH',
        poll_number,
        0x0001,              # partition: NOM_PART_OBJ
        NOM_MOC_PT_DEMOG,    # code: 42
        NOM_ATTR_GRP_PT_DEMOG  # 0x0807
    )
    return build_action(poll_number & 0xFFFF, NOM_MOC_VMS_MDS, NOM_ACT_POLL_MDIB_DATA, payload)

def build_mds_create_result(invoke_id: int, managed_obj: bytes, event_time: bytes) -> bytes:
    """MDS CREATE EVENT RESULT (réponse obligatoire au MDS Create Event)"""
    # EventReportResult
    evt_result = managed_obj + event_time + struct.pack('>HH', NOM_NOTI_MDS_CREAT, 0)
    rors_inner = struct.pack('>HHH', invoke_id, CMD_CONFIRMED_EVENT_REPORT, len(evt_result)) + evt_result
    rors = struct.pack('>HH', RORS_APDU, len(rors_inner)) + rors_inner
    return build_spdu(rors)

def build_assoc_request() -> bytes:
    """Construit le message Association Request complet"""
    user_data = ASSOC_REQ_USER_DATA
    # Longueur présentation header[1] = len(tout ce qui suit le 2e octet du pres header)
    inner = ASSOC_REQ_PRES_HEADER[2:] + user_data + ASSOC_REQ_PRES_TRAILER
    pres = bytes([ASSOC_REQ_PRES_HEADER[0], len(inner)]) + inner

    session_body = ASSOC_REQ_SESSION_DATA + pres
    session = bytes([ASSOC_REQ_SESSION_HEADER[0], len(session_body)]) + session_body
    return session

def parse_string_attr(data: bytes, offset: int, length: int) -> str:
    """Parse une String Philips (longueur u16 + contenu, null-terminé)"""
    if offset + 2 > len(data):
        return ''
    str_len = struct.unpack_from('>H', data, offset)[0]
    offset += 2
    if str_len == 0 or offset + str_len > len(data):
        return ''
    raw = data[offset:offset + str_len]
    return raw.rstrip(b'\x00').decode('utf-8', errors='replace').strip()

def parse_demographics(data: bytes) -> dict:
    """
    Parse un PollMdibDataReply pour extraire les attributs démographiques.
    Retourne un dict avec les champs patient.
    """
    result = {
        'timestamp': datetime.now().isoformat(),
        'demo_state': 'UNKNOWN',
        'given_name': '',
        'family_name': '',
        'patient_id': '',
        'sex': '',
        'patient_type': '',
    }

    try:
        # Cherche la PollInfoList dans le payload
        # Structure après ActionResult: PollMdibDataReply
        # poll_number(2) + rel_time(4) + abs_time(8) + TYPE(4) + attr_grp(2) + PollInfoList
        if len(data) < 20:
            return result

        offset = 0
        # poll_number
        offset += 2
        # rel_time_stamp
        offset += 4
        # abs_time_stamp (8 bytes, tous 0xFF normalement)
        offset += 8
        # polled_obj_type (TYPE = partition(2)+code(2))
        offset += 4
        # polled_attr_grp
        offset += 2

        # PollInfoList: count(2) + length(2)
        if offset + 4 > len(data):
            return result
        poll_count = struct.unpack_from('>H', data, offset)[0]
        offset += 4

        if poll_count == 0:
            log.debug("PollInfoList vide — patient non admis ?")
            return result

        # SingleContextPoll: context_id(2) + poll_info.count(2) + poll_info.length(2)
        offset += 2  # context_id
        obs_count = struct.unpack_from('>H', data, offset)[0]
        offset += 4  # count + length

        # ObservationPoll[]: handle(2) + AttributeList
        for _ in range(obs_count):
            if offset + 4 > len(data):
                break
            offset += 2  # obj_handle
            attr_count = struct.unpack_from('>H', data, offset)[0]
            offset += 2
            attr_length = struct.unpack_from('>H', data, offset)[0]
            offset += 2

            attr_end = offset + attr_length

            for _ in range(attr_count):
                if offset + 4 > len(data):
                    break
                attr_id = struct.unpack_from('>H', data, offset)[0]
                offset += 2
                val_len = struct.unpack_from('>H', data, offset)[0]
                offset += 2
                val_data = data[offset:offset + val_len]
                offset += val_len

                if attr_id == NOM_ATTR_PT_DEMOG_ST and val_len >= 2:
                    state = struct.unpack_from('>H', val_data)[0]
                    result['demo_state'] = PAT_DEMO_STATE.get(state, f'0x{state:04X}')

                elif attr_id == NOM_ATTR_PT_NAME_GIVEN:
                    result['given_name'] = parse_string_attr(val_data, 0, val_len)

                elif attr_id == NOM_ATTR_PT_NAME_FAMILY:
                    result['family_name'] = parse_string_attr(val_data, 0, val_len)

                elif attr_id == NOM_ATTR_PT_ID:
                    result['patient_id'] = parse_string_attr(val_data, 0, val_len)

                elif attr_id == NOM_ATTR_PT_SEX and val_len >= 2:
                    sex = struct.unpack_from('>H', val_data)[0]
                    result['sex'] = PAT_SEX.get(sex, f'0x{sex:04X}')

                elif attr_id == NOM_ATTR_PT_TYPE and val_len >= 2:
                    ptype = struct.unpack_from('>H', val_data)[0]
                    result['patient_type'] = PAT_TYPE.get(ptype, f'0x{ptype:04X}')

            offset = attr_end  # saute au prochain ObservationPoll proprement

    except Exception as e:
        log.warning(f"Erreur parsing démographiques : {e}")

    return result

def find_poll_reply_payload(data: bytes) -> bytes | None:
    """
    Extrait le payload PollMdibDataReply depuis un paquet UDP brut.
    Structure: SPpdu(4) + ROapdus(4) + RORSapdu(6) + ActionResult(10) + PollMdibDataReply
    """
    try:
        if len(data) < 24:
            return None
        # SPpdu: session_id(2) + p_context_id(2)
        offset = 4
        # ROapdus: ro_type(2) + length(2)
        ro_type = struct.unpack_from('>H', data, offset)[0]
        offset += 4
        if ro_type != RORS_APDU:
            return None
        # RORSapdu: invoke_id(2) + command_type(2) + length(2)
        cmd_type = struct.unpack_from('>H', data, offset + 2)[0]
        offset += 6
        if cmd_type != CMD_CONFIRMED_ACTION:
            return None
        # ActionResult: managed_object(6) + action_type(2) + length(2)
        offset += 10
        return data[offset:]
    except Exception:
        return None

def is_mds_create_event(data: bytes):
    """
    Détecte le MDS Create Event et retourne (invoke_id, managed_obj_bytes, event_time_bytes)
    ou None si ce n'est pas ce message.
    """
    try:
        if len(data) < 20:
            return None
        offset = 4  # saute SPpdu
        ro_type = struct.unpack_from('>H', data, offset)[0]
        if ro_type != ROIV_APDU:
            return None
        offset += 4
        invoke_id = struct.unpack_from('>H', data, offset)[0]
        cmd_type  = struct.unpack_from('>H', data, offset + 2)[0]
        offset += 6
        if cmd_type != CMD_CONFIRMED_EVENT_REPORT:
            return None
        # EventReportArgument: managed_object(6) + event_time(4) + event_type(2) + length(2)
        managed_obj = data[offset:offset + 6]
        event_time  = data[offset + 6:offset + 10]
        event_type  = struct.unpack_from('>H', data, offset + 10)[0]
        if event_type != NOM_NOTI_MDS_CREAT:
            return None
        return invoke_id, managed_obj, event_time
    except Exception:
        return None

# ─── Boucle principale ───────────────────────────────────────────────────────

def run(monitor_ip: str, output_file: str, interval: int = 30):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5.0)
    try:
        sock.bind(('', LOCAL_PORT))
    except OSError:
        sock.bind(('', 0))  # port auto si LOCAL_PORT occupé

    local_addr = sock.getsockname()
    log.info(f"Socket local : {local_addr}")
    log.info(f"Moniteur cible : {monitor_ip}:{MX800_DATA_PORT}")
    log.info(f"Sortie JSON : {output_file}")
    log.info(f"Intervalle polling : {interval}s")

    associated = False
    poll_number = 1
    last_poll = 0

    # Envoi Association Request
    assoc_req = build_assoc_request()
    log.info("Envoi Association Request...")
    sock.sendto(assoc_req, (monitor_ip, MX800_DATA_PORT))

    try:
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                log.debug(f"Reçu {len(data)} octets de {addr}")

                # Détection MDS Create Event → répondre obligatoirement
                mds = is_mds_create_event(data)
                if mds:
                    invoke_id, managed_obj, event_time = mds
                    log.info(f"MDS Create Event reçu (invoke_id={invoke_id}), envoi Result...")
                    result_msg = build_mds_create_result(invoke_id, managed_obj, event_time)
                    sock.sendto(result_msg, addr)
                    associated = True
                    last_poll = 0  # force poll immédiat
                    continue

                # Réception Association Response (0x0E en premier octet)
                if len(data) > 0 and data[0] == 0x0E:
                    log.info("Association Response reçue — association établie.")
                    associated = True
                    continue

                # Refuse (0x0C)
                if len(data) > 0 and data[0] == 0x0C:
                    log.warning("Association refusée par le moniteur. Nouvelle tentative dans 10s...")
                    time.sleep(10)
                    sock.sendto(assoc_req, (monitor_ip, MX800_DATA_PORT))
                    continue

                # Traitement Poll Result
                if associated:
                    payload = find_poll_reply_payload(data)
                    if payload is not None:
                        demo = parse_demographics(payload)
                        log.info(f"Démographiques : état={demo['demo_state']} | "
                                 f"nom={demo['family_name']} {demo['given_name']} | "
                                 f"ID={demo['patient_id']}")
                        with open(output_file, 'w') as f:
                            json.dump(demo, f, ensure_ascii=False, indent=2)

            except socket.timeout:
                pass  # normal, on continue

            # Envoi poll si associé et délai écoulé
            if associated and (time.time() - last_poll) >= interval:
                log.debug(f"Envoi poll démographique #{poll_number}...")
                msg = build_poll_demog(poll_number)
                sock.sendto(msg, (monitor_ip, MX800_DATA_PORT))
                poll_number = (poll_number + 1) & 0xFFFF
                last_poll = time.time()

    except KeyboardInterrupt:
        log.info("Arrêt demandé, envoi Release Request...")
        sock.sendto(RELEASE_REQ, (monitor_ip, MX800_DATA_PORT))
    finally:
        sock.close()
        log.info("Socket fermé.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Polling démographique MX800')
    parser.add_argument('--ip',       default='192.168.100.31',
                        help='IP du moniteur MX800 (défaut: 192.168.100.31)')
    parser.add_argument('--out',      default='/home/hegp/patient_demo.json',
                        help='Fichier JSON de sortie')
    parser.add_argument('--interval', type=int, default=30,
                        help='Intervalle de polling en secondes (défaut: 30)')
    parser.add_argument('--debug',    action='store_true',
                        help='Logs détaillés')
    args = parser.parse_args()

    if args.debug:
        log.setLevel(logging.DEBUG)

    run(args.ip, args.out, args.interval)
