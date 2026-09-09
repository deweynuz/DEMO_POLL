"""
Catalogue des ondes exportables — extrait automatiquement du guide rév. G.0,
tables « Waves » p. 179-188. NE PAS ÉDITER À LA MAIN : régénérer depuis le PDF.

Deux noms cohabitent pour une même onde et il faut les distinguer :
  - `nom`       : la nomenclature de l'observed value, ce qui identifie les
                  échantillons dans un SaObsValue (ex. NOM_PLETH, 0x4BB4) ;
  - `label_nom` : la nomenclature du label, ce qu'on met dans la liste de
                  priorité (ex. NLS_NOM_PULS_OXIM_PLETH, 0x00024BB4).
Ne sont retenues que les entrées vérifiant label = 0x00020000 | physio_id,
c'est-à-dire la partition SCADA. Les labels EMFC de la p. 188 (partition
0x0401xxxx) pointent parfois vers le même physio_id et sont écartés.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Onde:
    nom: str          # observed value, identifie les échantillons (SaObsValue, p. 87)
    label_nom: str    # label, sert à la liste de priorité (SET PRIORITY LIST, p. 64)
    physio_id: int
    label: int        # TextId
    unite: int        # code de la partition dimension, 0 si non spécifié
    unite_nom: str


CATALOGUE: dict[int, Onde] = {
    0x0100: Onde('NOM_ECG_ELEC_POTL', 'NLS_NOM_ECG_ELEC_POTL', 0x0100, 0x00020100, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x0101: Onde('NOM_ECG_ELEC_POTL_I', 'NLS_NOM_ECG_ELEC_POTL_I', 0x0101, 0x00020101, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x0102: Onde('NOM_ECG_ELEC_POTL_II', 'NLS_NOM_ECG_ELEC_POTL_II', 0x0102, 0x00020102, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x0103: Onde('NOM_ECG_ELEC_POTL_V1', 'NLS_NOM_ECG_ELEC_POTL_V1', 0x0103, 0x00020103, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x0104: Onde('NOM_ECG_ELEC_POTL_V2', 'NLS_NOM_ECG_ELEC_POTL_V2', 0x0104, 0x00020104, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x0105: Onde('NOM_ECG_ELEC_POTL_V3', 'NLS_NOM_ECG_ELEC_POTL_V3', 0x0105, 0x00020105, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x0106: Onde('NOM_ECG_ELEC_POTL_V4', 'NLS_NOM_ECG_ELEC_POTL_V4', 0x0106, 0x00020106, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x0107: Onde('NOM_ECG_ELEC_POTL_V5', 'NLS_NOM_ECG_ELEC_POTL_V5', 0x0107, 0x00020107, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x0108: Onde('NOM_ECG_ELEC_POTL_V6', 'NLS_NOM_ECG_ELEC_POTL_V6', 0x0108, 0x00020108, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x013D: Onde('NOM_ECG_ELEC_POTL_III', 'NLS_NOM_ECG_ELEC_POTL_III', 0x013D, 0x0002013D, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x013E: Onde('NOM_ECG_ELEC_POTL_AVR', 'NLS_NOM_ECG_ELEC_POTL_AVR', 0x013E, 0x0002013E, 0x0000, ''),
    0x013F: Onde('NOM_ECG_ELEC_POTL_AVL', 'NLS_NOM_ECG_ELEC_POTL_AVL', 0x013F, 0x0002013F, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x0140: Onde('NOM_ECG_ELEC_POTL_AVF', 'NLS_NOM_ECG_ELEC_POTL_AVF', 0x0140, 0x00020140, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x0143: Onde('NOM_ECG_ELEC_POTL_V', 'NLS_NOM_ECG_ELEC_POTL_V', 0x0143, 0x00020143, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x014B: Onde('NOM_ECG_ELEC_POTL_MCL', 'NLS_NOM_ECG_ELEC_POTL_MCL', 0x014B, 0x0002014B, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x014C: Onde('NOM_ECG_ELEC_POTL_MCL1', 'NLS_NOM_ECG_ELEC_POTL_MCL1', 0x014C, 0x0002014C, 0x10B2, 'NOM_DIM_MILLI_VOLT'),
    0x4A00: Onde('NOM_PRESS_BLD', 'NLS_NOM_PRESS_BLD', 0x4A00, 0x00024A00, 0x0F20, 'NOM_DIM_MMHG'),
    0x4A0C: Onde('NOM_PRESS_BLD_AORT', 'NLS_NOM_PRESS_BLD_AORT', 0x4A0C, 0x00024A0C, 0x0F20, 'NOM_DIM_MMHG'),
    0x4A10: Onde('NOM_PRESS_BLD_ART', 'NLS_NOM_PRESS_BLD_ART', 0x4A10, 0x00024A10, 0x0F20, 'NOM_DIM_MMHG'),
    0x4A14: Onde('NOM_PRESS_BLD_ART_ABP', 'NLS_NOM_PRESS_BLD_ART_ABP', 0x4A14, 0x00024A14, 0x0F20, 'NOM_DIM_MMHG'),
    0x4A1C: Onde('NOM_PRESS_BLD_ART_PULM', 'NLS_NOM_PRESS_BLD_ART_PULM', 0x4A1C, 0x00024A1C, 0x0F20, 'NOM_DIM_MMHG'),
    0x4A28: Onde('NOM_PRESS_BLD_ART_UMB', 'NLS_NOM_PRESS_BLD_ART_UMB', 0x4A28, 0x00024A28, 0x0F20, 'NOM_DIM_MMHG'),
    0x4A30: Onde('NOM_PRESS_BLD_ATR_LEFT', 'NLS_NOM_PRESS_BLD_ATR_LEFT', 0x4A30, 0x00024A30, 0x0F20, 'NOM_DIM_MMHG'),
    0x4A34: Onde('NOM_PRESS_BLD_ATR_RIGHT', 'NLS_NOM_PRESS_BLD_ATR_RIGHT', 0x4A34, 0x00024A34, 0x0F20, 'NOM_DIM_MMHG'),
    0x4A44: Onde('NOM_PRESS_BLD_VEN_CENT', 'NLS_NOM_PRESS_BLD_VEN_CENT', 0x4A44, 0x00024A44, 0x0F20, 'NOM_DIM_MMHG'),
    0x4A48: Onde('NOM_PRESS_BLD_VEN_UMB', 'NLS_NOM_PRESS_BLD_VEN_UMB', 0x4A48, 0x00024A48, 0x0F20, 'NOM_DIM_MMHG'),
    0x4BB4: Onde('NOM_PLETH', 'NLS_NOM_PULS_OXIM_PLETH', 0x4BB4, 0x00024BB4, 0x0200, 'NOM_DIM_DIMLESS'),
    0x5000: Onde('NOM_RESP', 'NLS_NOM_RESP', 0x5000, 0x00025000, 0x10C0, 'NOM_DIM_X_OHM'),
    0x50AC: Onde('NOM_AWAY_CO2', 'NLS_NOM_AWAY_CO2', 0x50AC, 0x000250AC, 0x0F20, 'NOM_DIM_MMHG'),
    0x50D4: Onde('NOM_FLOW_AWAY', 'NLS_NOM_FLOW_AWAY', 0x50D4, 0x000250D4, 0x0000, ''),
    0x50F0: Onde('NOM_PRESS_AWAY', 'NLS_NOM_PRESS_AWAY', 0x50F0, 0x000250F0, 0x0000, ''),
    0x5108: Onde('NOM_PRESS_AWAY_INSP', 'NLS_NOM_PRESS_AWAY_INSP', 0x5108, 0x00025108, 0x0000, ''),
    0x5164: Onde('NOM_CONC_AWAY_O2', 'NLS_NOM_CONC_AWAY_O2', 0x5164, 0x00025164, 0x0F20, 'NOM_DIM_MMHG'),
    0x518C: Onde('NOM_VENT_FLOW_INSP', 'NLS_NOM_VENT_FLOW_INSP', 0x518C, 0x0002518C, 0x0000, ''),
    0x51D8: Onde('NOM_CONC_AWAY_DESFL', 'NLS_NOM_CONC_AWAY_DESFL', 0x51D8, 0x000251D8, 0x0F20, 'NOM_DIM_MMHG'),
    0x51DC: Onde('NOM_CONC_AWAY_ENFL', 'NLS_NOM_CONC_AWAY_ENFL', 0x51DC, 0x000251DC, 0x0F20, 'NOM_DIM_MMHG'),
    0x51E0: Onde('NOM_CONC_AWAY_HALOTH', 'NLS_NOM_CONC_AWAY_HALOTH', 0x51E0, 0x000251E0, 0x0F20, 'NOM_DIM_MMHG'),
    0x51E4: Onde('NOM_CONC_AWAY_SEVOFL', 'NLS_NOM_CONC_AWAY_SEVOFL', 0x51E4, 0x000251E4, 0x0F20, 'NOM_DIM_MMHG'),
    0x51E8: Onde('NOM_CONC_AWAY_ISOFL', 'NLS_NOM_CONC_AWAY_ISOFL', 0x51E8, 0x000251E8, 0x0F20, 'NOM_DIM_MMHG'),
    0x51F0: Onde('NOM_CONC_AWAY_N2O', 'NLS_NOM_CONC_AWAY_N2O', 0x51F0, 0x000251F0, 0x0F20, 'NOM_DIM_MMHG'),
    0x537C: Onde('NOM_CONC_AWAY_N2', 'NLS_NOM_CONC_AWAY_N2', 0x537C, 0x0002537C, 0x0F20, 'NOM_DIM_MMHG'),
    0x5388: Onde('NOM_CONC_AWAY_AGENT', 'NLS_NOM_CONC_AWAY_AGENT', 0x5388, 0x00025388, 0x0F20, 'NOM_DIM_MMHG'),
    0x5808: Onde('NOM_PRESS_INTRA_CRAN', 'NLS_NOM_PRESS_INTRA_CRAN', 0x5808, 0x00025808, 0x0F20, 'NOM_DIM_MMHG'),
    0x592C: Onde('NOM_EEG_ELEC_POTL_CRTX', 'NLS_NOM_EEG_ELEC_POTL_CRTX', 0x592C, 0x0002592C, 0x10B3, 'NOM_DIM_MICRO_VOLT'),
    0xF08C: Onde('NOM_PULS_OXIM_PLETH_RIGHT', 'NLS_NOM_PULS_OXIM_PLETH_RIGHT', 0xF08C, 0x0002F08C, 0x0200, 'NOM_DIM_DIMLESS'),
    0xF08D: Onde('NOM_PULS_OXIM_PLETH_LEFT', 'NLS_NOM_PULS_OXIM_PLETH_LEFT', 0xF08D, 0x0002F08D, 0x0200, 'NOM_DIM_DIMLESS'),
    0xF09B: Onde('NOM_PULS_OXIM_PLETH_TELE', 'NLS_NOM_PULS_OXIM_PLETH_TELE', 0xF09B, 0x0002F09B, 0x0200, 'NOM_DIM_DIMLESS'),
    0xF0A4: Onde('NOM_PRESS_GEN_1', 'NLS_NOM_PRESS_GEN_1', 0xF0A4, 0x0002F0A4, 0x0F20, 'NOM_DIM_MMHG'),
    0xF0A8: Onde('NOM_PRESS_GEN_2', 'NLS_NOM_PRESS_GEN_2', 0xF0A8, 0x0002F0A8, 0x0F20, 'NOM_DIM_MMHG'),
    0xF0AC: Onde('NOM_PRESS_GEN_3', 'NLS_NOM_PRESS_GEN_3', 0xF0AC, 0x0002F0AC, 0x0F20, 'NOM_DIM_MMHG'),
    0xF0B0: Onde('NOM_PRESS_GEN_4', 'NLS_NOM_PRESS_GEN_4', 0xF0B0, 0x0002F0B0, 0x0F20, 'NOM_DIM_MMHG'),
    0xF0B4: Onde('NOM_PRESS_INTRA_CRAN_1', 'NLS_NOM_PRESS_INTRA_CRAN_1', 0xF0B4, 0x0002F0B4, 0x0F20, 'NOM_DIM_MMHG'),
    0xF0B8: Onde('NOM_PRESS_INTRA_CRAN_2', 'NLS_NOM_PRESS_INTRA_CRAN_2', 0xF0B8, 0x0002F0B8, 0x0F20, 'NOM_DIM_MMHG'),
    0xF0BC: Onde('NOM_PRESS_BLD_ART_FEMORAL', 'NLS_NOM_PRESS_BLD_ART_FEMORAL', 0xF0BC, 0x0002F0BC, 0x0F20, 'NOM_DIM_MMHG'),
    0xF0C0: Onde('NOM_PRESS_BLD_ART_BRACHIAL', 'NLS_NOM_PRESS_BLD_ART_BRACHIAL', 0xF0C0, 0x0002F0C0, 0x0F20, 'NOM_DIM_MMHG'),
}


PAR_NOM: dict[str, Onde] = {}
for _o in CATALOGUE.values():
    PAR_NOM[_o.nom] = _o
    PAR_NOM[_o.label_nom] = _o


def onde(designation: int | str) -> Onde | None:
    """Retrouve une onde par physio_id, par nom d'observed value ou par nom de label."""
    if isinstance(designation, int):
        return CATALOGUE.get(designation)
    return PAR_NOM.get(designation)


def est_ecg(physio_id: int) -> bool:
    """
    Les ondes ECG sont limitées à 3 simultanées, les autres à 8 (p. 286-287).
    Les potentiels d'électrode ECG occupent la plage 0x0100-0x014F.
    """
    return 0x0100 <= physio_id <= 0x014F


def verifier_selection(physio_ids: list[int]) -> list[str]:
    """
    Contrôle une sélection d'ondes AVANT de l'envoyer. Le moniteur ignore
    silencieusement toute entrée invalide ou en excès (p. 287) : mieux vaut
    savoir tout de suite ce qui ne passera pas.
    """
    problemes = []
    inconnues = [p for p in physio_ids if p not in CATALOGUE]
    if inconnues:
        problemes.append("ondes absentes du catalogue : "
                         + ', '.join(f'0x{p:04X}' for p in inconnues))
    if len(set(physio_ids)) != len(physio_ids):
        problemes.append("doublons dans la sélection")
    n_ecg = sum(1 for p in physio_ids if est_ecg(p))
    if n_ecg > 3:
        problemes.append(f"{n_ecg} ondes ECG demandées, maximum 3 (p. 286-287)")
    if len(physio_ids) - n_ecg > 8:
        problemes.append(f"{len(physio_ids)-n_ecg} ondes non-ECG demandées, "
                         f"maximum 8 (p. 286-287)")
    return problemes
