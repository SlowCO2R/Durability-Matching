# -*- coding: utf-8 -*-
"""
Match Gamry durability data with GC injection timestamps, then compute
Faradaic Efficiency (FE), Single-Pass Conversion Efficiency (SPCE),
and CO2 crossover vs hypothetical O2.

v2 adds:
  - FE_CO and FE_H2 calculations (Chan1 and Chan2 CO)
  - SPCE calculation
  - CO2 crossover vs hypothetical O2 (from Faraday's law)
  - Per-gas MFC correction factors (Alicat viscosity-based at 25 deg C)
  - "Calculated Results" sheets (Chan1 and Chan2)
  - "Formulas" sheet with all equations
  - "Metadata" sheet with constants
  - Publication-quality plots (ACS/Nature standards)

See Durability_GC_Matched_README.txt for Alicat references and correction
factor derivations.

Author: tpham + Claude
"""

import os
import re
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy import stats
from openpyxl import load_workbook
from openpyxl.styles import PatternFill

# ============================================================
# ====================== CONFIGURATION =======================
# ============================================================

ROOT_FOLDER = r"Y:\5900\HydrogenTechFuelCellsGroup\CO2R\Nhan P\Experiments\CO2 Cell Testing\TS2\2NP66_RR_25cm2"

DURABILITY_SUBFOLDER = "Durability Data"  # Change this to match your folder name
DURABILITY_FOLDER = os.path.join(ROOT_FOLDER, DURABILITY_SUBFOLDER)
COMBINED_EXCEL    = os.path.join(ROOT_FOLDER, "output", "Combined_LV_GC_Analysis.xlsx")
OUTPUT_EXCEL      = os.path.join(ROOT_FOLDER, "output", "Durability_GC_Matched_v2.xlsx")
OUTPUT_PLOT_DIR   = os.path.join(ROOT_FOLDER, "output")

cell_area_cm2 = 25
RE = 0  # reference electrode offset

# --- Physical / experiment constants ---
FARADAY_CONSTANT = 96485.33289     # C/mol
GAS_CONSTANT     = 0.083144626     # L*bar/(K*mol)
SURFACE_AREA_CM2 = 25.0

# --- SLPM standard conditions (for converting SLPM to mol) ---
P_STP_BAR = 1.01325               # bar (101.325 kPa = 1 atm)
T_STP_K   = 273.15                # K (0 deg C)

# --- MFC correction factors (CO2-calibrated Alicat, viscosity at 25 deg C) ---
CF_CO2 = 1.00   # CO2: 149.3 uP -> ratio 1.00
CF_N2  = 1.19   # N2:  178.1 uP -> ratio 1.19
CF_CO  = 1.19   # CO:  ~177  uP -> ratio 1.19
CF_H2  = 0.60   # H2:   89.2 uP -> ratio 0.60
CF_O2  = 1.37   # O2:  204.6 uP -> ratio 1.37

# --- Electrons transferred per molecule ---
N_ELECTRONS_CO = 2   # CO2 + 2H+ + 2e- -> CO + H2O
N_ELECTRONS_H2 = 2   # 2H+ + 2e- -> H2

# QC thresholds
MIN_POINTS_THRESHOLD  = 30
COVERAGE_THRESHOLD    = 0.50
POTENTIAL_STD_LIMIT   = 0.5

# Color fills for QC
GREEN_FILL  = "C6EFCE"
RED_FILL    = "FFC7CE"
YELLOW_FILL = "FFEB9C"

# ============================================================
# =================== DTA HEADER PARSING =====================
# ============================================================

def extract_header_datetime(filepath):
    date_str = time_str = None
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if line.startswith('DATE'):
                date_str = line.split('\t')[2].strip()
            elif line.startswith('TIME') and time_str is None:
                time_str = line.split('\t')[2].strip()
            if date_str and time_str:
                break
    if not date_str or not time_str:
        raise ValueError(f"Could not find DATE/TIME in header of {filepath}")
    return datetime.strptime(f"{date_str} {time_str}", "%m/%d/%Y %H:%M:%S")


# ============================================================
# ================ DTA DATA PARSING (reused) =================
# ============================================================

def safe_get(df, key, default=0):
    if df is None:
        return default
    if isinstance(df, pd.DataFrame):
        return df[key] if key in df.columns else pd.Series([default] * len(df), index=df.index)
    try:
        return df.get(key, default)
    except Exception:
        return default


def parse_dta_file(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        curve_start = None
        n_points = None
        for i, line in enumerate(lines):
            if line.startswith("CURVE") or line.startswith("GALVANOSTATIC"):
                parts = line.strip().split('\t')
                if len(parts) >= 3 and parts[1].upper() == "TABLE":
                    try:
                        n_points = int(parts[2])
                        curve_start = i
                        break
                    except Exception:
                        continue
        if curve_start is None:
            return None, None
        headers = lines[curve_start + 1].strip().split('\t')
        data_lines = lines[curve_start + 3:curve_start + 3 + n_points]
        data = [line.strip().split('\t') for line in data_lines]
        df = pd.DataFrame(data, columns=headers)
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df, headers
    except Exception as e:
        print(f"[ERROR] parse_dta_file {filepath}: {e}")
        return None, None


def parse_pwrgalveismon(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
        data_start = None
        header_line = None
        for i, line in enumerate(lines):
            if line.strip().upper().startswith('ZCURVE'):
                data_start = i
                header_line = lines[i + 1].strip()
                break
            if all(tok in line for tok in ['Time', 'Vdc', 'Idc']):
                data_start = i
                header_line = lines[i].strip()
                break
        if data_start is None or header_line is None:
            return None
        header_cols = header_line.split('\t')
        data_lines = lines[data_start + 2:]
        data = [line.strip().split('\t') for line in data_lines if line.strip()]
        if not data:
            return None
        df = pd.DataFrame(data, columns=header_cols[:len(data[0])])
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        return df
    except Exception as e:
        print(f"[ERROR] parse_pwrgalveismon {filepath}: {e}")
        return None


def process_gstatic(filepath):
    df, _ = parse_dta_file(filepath)
    if df is None:
        return None
    out = pd.DataFrame(index=df.index)
    out['Time (s)'] = safe_get(df, 'T', 0)
    out['Current Density (mA/cm2)'] = -safe_get(df, 'Im', 0) / cell_area_cm2 * 1000
    out['Potential (V)'] = -safe_get(df, 'Vf', 0)
    out['Anode vs RHE (V)'] = safe_get(df, 'Vf1', 0) - RE
    out['Cathode vs RHE (V)'] = np.abs(safe_get(df, 'Vf2', 0) - RE)
    out['HFR (Ohm.cm2)'] = 0.0
    out['Zimag'] = 0.0
    out['Source File'] = os.path.basename(filepath)
    return out.reset_index(drop=True)


def process_eismon(filepath):
    df = parse_pwrgalveismon(filepath)
    if df is None:
        return None
    out = pd.DataFrame(index=df.index)
    time_series = safe_get(df, 'Time', None)
    if isinstance(time_series, pd.Series) and not time_series.isnull().all():
        out['Time (s)'] = time_series
    else:
        out['Time (s)'] = safe_get(df, 'T', 0)
    out['Potential (V)'] = -safe_get(df, 'Vdc', 0)
    out['Current Density (mA/cm2)'] = -safe_get(df, 'Idc', 0) / cell_area_cm2 * 1000
    out['HFR (Ohm.cm2)'] = safe_get(df, 'Zreal', 0) * cell_area_cm2
    out['Zimag'] = safe_get(df, 'Zimag', 0)
    out['Source File'] = os.path.basename(filepath)

    def try_merge_aech(out_df, col_name, suffix):
        aech_file = str(Path(filepath).parent / (Path(filepath).stem + suffix + ".DTA"))
        if os.path.exists(aech_file):
            aech_df = parse_pwrgalveismon(aech_file)
            if aech_df is not None and 'Time' in aech_df.columns and 'Vdc' in aech_df.columns:
                tmp = aech_df[['Time', 'Vdc']].rename(columns={'Vdc': col_name})
                tmp[col_name] = tmp[col_name] - RE
                merged = pd.merge_asof(
                    out_df.sort_values("Time (s)").reset_index(drop=True),
                    tmp.rename(columns={'Time': 'Time (s)'}).sort_values("Time (s)"),
                    on="Time (s)", direction="nearest")
                return merged
            else:
                out_df[col_name] = 0.0
                return out_df
        else:
            out_df[col_name] = 0.0
            return out_df

    out = try_merge_aech(out, 'Anode vs RHE (V)', "_AECH1")
    out = try_merge_aech(out, 'Cathode vs RHE (V)', "_AECH2")
    if 'Cathode vs RHE (V)' in out.columns:
        out['Cathode vs RHE (V)'] = np.abs(out['Cathode vs RHE (V)'])
    out = out.fillna(0)
    return out.reset_index(drop=True)


# ============================================================
# ============ STEP 1: BUILD TIMESTAMPED TIMELINE ============
# ============================================================

def build_timestamped_timeline(durability_folder):
    folder = Path(durability_folder)
    all_files = sorted(
        [p for p in folder.iterdir()
         if p.suffix.upper() == '.DTA'
         and not '_AECH' in p.stem.upper()],
        key=lambda x: x.stat().st_mtime)

    print(f"Found {len(all_files)} DTA files in {durability_folder}")
    frames = []
    for filepath in all_files:
        filename = filepath.name
        filename_upper = filename.upper()
        if filename_upper.startswith("GSTATIC") or filename_upper.startswith("PWRGSTATIC"):
            df = process_gstatic(str(filepath))
        elif filename_upper.startswith("PWRGALVEISMON"):
            df = process_eismon(str(filepath))
        else:
            continue
        if df is None or df.empty:
            print(f"  [SKIP] {filename} - no data")
            continue
        header_start = extract_header_datetime(str(filepath))
        df['Time (s)'] = pd.to_numeric(df['Time (s)'], errors='coerce').fillna(0)
        df['Absolute_Datetime'] = header_start + pd.to_timedelta(df['Time (s)'], unit='s')
        frames.append(df)
        print(f"  [OK] {filename} -> start={header_start}, rows={len(df)}")

    if not frames:
        raise ValueError("No DTA files were successfully parsed")
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values('Absolute_Datetime').reset_index(drop=True)
    print(f"\nCombined timeline: {len(combined)} rows, "
          f"{combined['Absolute_Datetime'].min()} to {combined['Absolute_Datetime'].max()}")
    return combined


# ============================================================
# ====== STEP 2-4: MATCH, AVERAGE, AND QC CHECK =============
# ============================================================

GAMRY_AVG_COLS = [
    'Potential (V)',
    'Current Density (mA/cm2)',
    'Anode vs RHE (V)',
    'Cathode vs RHE (V)',
    'HFR (Ohm.cm2)',
    'Zimag',
]


def match_and_average(gamry_df, matched_dur_df):
    results = []
    for _, gc_row in matched_dur_df.iterrows():
        w_start = gc_row['LV_Preceding_WindowStart']
        w_end   = gc_row['LV_Preceding_WindowEnd']
        mask = (gamry_df['Absolute_Datetime'] >= w_start) & \
               (gamry_df['Absolute_Datetime'] <= w_end)
        window_data = gamry_df.loc[mask]
        row_out = gc_row.to_dict()
        n_points = len(window_data)
        row_out['Gamry_N_Points'] = n_points
        window_duration = (w_end - w_start).total_seconds()
        if n_points >= 2 and window_duration > 0:
            data_span = (window_data['Absolute_Datetime'].max() -
                         window_data['Absolute_Datetime'].min()).total_seconds()
            coverage = data_span / window_duration
        else:
            coverage = 0.0
        row_out['Gamry_Coverage_%'] = round(coverage * 100, 1)
        if n_points > 0:
            for col in GAMRY_AVG_COLS:
                if col in window_data.columns:
                    row_out[f'Gamry_Avg_{col}'] = window_data[col].mean()
                    row_out[f'Gamry_Std_{col}'] = window_data[col].std()
        else:
            for col in GAMRY_AVG_COLS:
                row_out[f'Gamry_Avg_{col}'] = np.nan
                row_out[f'Gamry_Std_{col}'] = np.nan
        qc_issues = []
        if n_points == 0:
            qc_issues.append("No Gamry data in window")
        elif n_points < MIN_POINTS_THRESHOLD:
            qc_issues.append(f"Low sample count: {n_points}")
        if coverage < COVERAGE_THRESHOLD and n_points > 0:
            qc_issues.append(f"Low coverage: {coverage*100:.0f}%")
        pot_std = row_out.get(f'Gamry_Std_Potential (V)', np.nan)
        if np.isfinite(pot_std) and pot_std > POTENTIAL_STD_LIMIT:
            qc_issues.append(f"High V std: {pot_std:.3f}V")
        row_out['Match_QC'] = "; ".join(qc_issues) if qc_issues else "OK"
        results.append(row_out)
    return pd.DataFrame(results)


# ============================================================
# ======= STEP 5: FE, SPCE, AND CROSSOVER CALCULATIONS ======
# ============================================================

def compute_fe_spce_crossover(matched_df, gamry_df=None,
                               co_col='Cathode_Carbon_Monoxide_Chan1',
                               label='Chan1'):
    """
    Compute FE, SPCE, crossover for each matched row.
    co_col selects which CO channel to use.
    """
    results = []
    h2_col = 'Cathode_Hydrogen_Chan1'
    outlet_flow_col = 'LV_outlet_flow_FB_Mean'
    inlet_flow_col  = 'LV_C_CO2_HI_FB_Mean'

    required = [(co_col, 'CO'), (h2_col, 'H2'),
                (outlet_flow_col, 'Outlet Flow'), (inlet_flow_col, 'Inlet Flow')]
    missing = []
    for col_name, col_var in required:
        if col_name not in matched_df.columns:
            missing.append(f"{col_var} (expected '{col_name}')")
    if missing:
        print(f"[WARNING] Missing columns for {label}: {', '.join(missing)}")
        return pd.DataFrame()

    print(f"  [{label}] CO='{co_col}', H2='{h2_col}'")

    for _, row in matched_df.iterrows():
        calc = {}
        calc['GC_Datetime'] = row.get('GC_Datetime', '')
        calc['GC_SampleName'] = row.get('GC_SampleName', row.get('Sample_Name', ''))
        calc['Match_QC'] = row.get('Match_QC', '')

        cd_mA = row.get('Gamry_Avg_Current Density (mA/cm2)', np.nan)
        cd_A = abs(cd_mA) / 1000.0 if np.isfinite(cd_mA) else np.nan

        co_pct = pd.to_numeric(row.get(co_col, np.nan), errors='coerce')
        h2_pct = pd.to_numeric(row.get(h2_col, np.nan), errors='coerce')
        indicated_flow = pd.to_numeric(row.get(outlet_flow_col, np.nan), errors='coerce')
        inlet_flow = pd.to_numeric(row.get(inlet_flow_col, np.nan), errors='coerce')

        calc['Current_Density_A_cm2'] = cd_A
        calc['CO_mol%'] = co_pct
        calc['H2_mol%'] = h2_pct
        calc['MFC_Outlet_Flow_SLPM'] = indicated_flow
        calc['MFC_Inlet_Flow_SLPM'] = inlet_flow
        calc['Gamry_Avg_Potential (V)'] = row.get('Gamry_Avg_Potential (V)', np.nan)
        calc['Gamry_Std_Potential (V)'] = row.get('Gamry_Std_Potential (V)', np.nan)

        nan_cols = ['Charge_per_min_C', 'Corrected_Flow_SLPM',
                    'FE_CO_%', 'FE_H2_%', 'FE_Total_%', 'FE_Mismatch_%',
                    'SPCE_%', 'Hypothetical_O2_SLPM',
                    'Utilized_CO2_SLPM', 'CO2_Outlet_SLPM',
                    'CO2_Crossover_SLPM', 'Crossover_Ratio']

        if not np.isfinite(cd_A) or not np.isfinite(co_pct) or not np.isfinite(h2_pct) \
                or not np.isfinite(indicated_flow) or not np.isfinite(inlet_flow):
            for c in nan_cols:
                calc[c] = np.nan
            calc['Calc_QC'] = 'Missing input data'
            results.append(calc)
            continue

        # Charge per minute (C/min)
        charge_per_min = cd_A * SURFACE_AREA_CM2 * 60.0
        calc['Charge_per_min_C'] = charge_per_min

        # Corrected outlet gas flow (SLPM)
        co_frac  = co_pct / 100.0
        h2_frac  = h2_pct / 100.0
        co2_frac = (100.0 - co_pct - h2_pct) / 100.0

        cf_mix = CF_CO * co_frac + CF_H2 * h2_frac + CF_CO2 * co2_frac
        corrected_flow = indicated_flow / cf_mix
        calc['Corrected_Flow_SLPM'] = corrected_flow

        # Faradaic Efficiency (%)
        if charge_per_min > 0:
            molar_flow = (P_STP_BAR * corrected_flow) / (GAS_CONSTANT * T_STP_K)
            fe_co = molar_flow * co_frac * N_ELECTRONS_CO * FARADAY_CONSTANT / charge_per_min * 100.0
            fe_h2 = molar_flow * h2_frac * N_ELECTRONS_H2 * FARADAY_CONSTANT / charge_per_min * 100.0
        else:
            fe_co = np.nan
            fe_h2 = np.nan

        calc['FE_CO_%'] = fe_co
        calc['FE_H2_%'] = fe_h2
        fe_total = (fe_co + fe_h2) if (np.isfinite(fe_co) and np.isfinite(fe_h2)) else np.nan
        calc['FE_Total_%'] = fe_total
        calc['FE_Mismatch_%'] = (fe_total - 100.0) if np.isfinite(fe_total) else np.nan

        # Hypothetical O2 production (SLPM) from Faraday's law
        # No MFC correction needed — this is a theoretical value, not a measured flow
        mol_o2_per_min = charge_per_min / (4.0 * FARADAY_CONSTANT)
        hypothetical_o2_slpm = mol_o2_per_min * (GAS_CONSTANT * T_STP_K) / P_STP_BAR
        calc['Hypothetical_O2_SLPM'] = hypothetical_o2_slpm

        # Utilized CO2 (SLPM) — only CO, not H2
        utilized_co2 = corrected_flow * co_frac
        calc['Utilized_CO2_SLPM'] = utilized_co2

        # CO2 outlet flow (SLPM)
        co2_outlet = corrected_flow * co2_frac
        calc['CO2_Outlet_SLPM'] = co2_outlet

        # CO2 crossover (SLPM) — carbon balance
        co2_crossover = inlet_flow - (utilized_co2 + co2_outlet)
        calc['CO2_Crossover_SLPM'] = co2_crossover

        # SPCE (%)
        if inlet_flow > 0:
            spce = (corrected_flow * co_frac) / inlet_flow * 100.0
        else:
            spce = np.nan
        calc['SPCE_%'] = spce

        # Crossover ratio (CO2 crossover / hypothetical O2)
        if hypothetical_o2_slpm > 0:
            crossover_ratio = co2_crossover / hypothetical_o2_slpm
        else:
            crossover_ratio = np.nan
        calc['Crossover_Ratio'] = crossover_ratio

        # Calculation QC
        calc_issues = []
        if np.isfinite(fe_co) and np.isfinite(fe_h2):
            if fe_total > 110:
                calc_issues.append(f"FE_total={fe_total:.1f}% (>110%)")
            if fe_co < 0 or fe_h2 < 0:
                calc_issues.append("Negative FE")
        if np.isfinite(spce) and spce > 100:
            calc_issues.append(f"SPCE={spce:.1f}% (>100%)")
        if np.isfinite(co2_crossover) and co2_crossover < -0.01:
            calc_issues.append(f"Negative CO2 crossover ({co2_crossover:.4f})")
        calc['Calc_QC'] = "; ".join(calc_issues) if calc_issues else "OK"

        results.append(calc)

    df_out = pd.DataFrame(results)
    # Add elapsed durability-only time in hours (excludes break-in gaps)
    if gamry_df is not None:
        df_out.insert(2, 'Time_h', compute_durability_only_hours(
            df_out['GC_Datetime'], gamry_df))
    else:
        df_out.insert(2, 'Time_h', compute_time_hours(df_out, 'GC_Datetime'))
    return df_out


# ============================================================
# =============== EXPORT WITH COLOR CODING ===================
# ============================================================

def color_qc_column(ws, col_name='Match_QC'):
    qc_col_idx = None
    for idx, cell in enumerate(ws[1], start=1):
        if cell.value == col_name:
            qc_col_idx = idx
            break
    if qc_col_idx is None:
        return
    green = PatternFill(start_color=GREEN_FILL, end_color=GREEN_FILL, fill_type="solid")
    red   = PatternFill(start_color=RED_FILL,   end_color=RED_FILL,   fill_type="solid")
    for row in ws.iter_rows(min_row=2, min_col=qc_col_idx, max_col=qc_col_idx):
        for cell in row:
            if cell.value == "OK":
                cell.fill = green
            elif cell.value:
                cell.fill = red


def color_mismatch_column(ws):
    """Highlight FE_Mismatch_% cells: green if abs < 5, yellow 5-10, red > 10."""
    col_idx = None
    for idx, cell in enumerate(ws[1], start=1):
        if cell.value == 'FE_Mismatch_%':
            col_idx = idx
            break
    if col_idx is None:
        return
    green  = PatternFill(start_color=GREEN_FILL,  end_color=GREEN_FILL,  fill_type="solid")
    yellow = PatternFill(start_color=YELLOW_FILL, end_color=YELLOW_FILL, fill_type="solid")
    red    = PatternFill(start_color=RED_FILL,    end_color=RED_FILL,    fill_type="solid")
    for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
        for cell in row:
            if cell.value is not None:
                try:
                    val = abs(float(cell.value))
                    if val < 5:
                        cell.fill = green
                    elif val < 10:
                        cell.fill = yellow
                    else:
                        cell.fill = red
                except (ValueError, TypeError):
                    pass


# ============================================================
# ================ PUBLICATION PLOT STYLING ==================
# ============================================================

# ACS/Nature style: Arial font, inside ticks, no top/right ticks
PLOT_PARAMS = {
    'font.family': 'Arial',
    'font.size': 14,
    'axes.labelsize': 16,
    'axes.titlesize': 16,
    'xtick.labelsize': 13,
    'ytick.labelsize': 13,
    'axes.linewidth': 1.5,
    'xtick.major.width': 1.5,
    'ytick.major.width': 1.5,
    'xtick.major.size': 6,
    'ytick.major.size': 6,
    'xtick.direction': 'in',
    'ytick.direction': 'in',
    'xtick.top': False,
    'ytick.right': False,
    'axes.spines.top': True,
    'axes.spines.right': True,
    'legend.fontsize': 12,
    'legend.frameon': False,
    'figure.dpi': 300,
    'savefig.dpi': 300,
    'savefig.bbox': 'tight',
}

LINE_WIDTH = 2
MARKER_SIZE = 8
MARKER_EDGE = 1.5
FE_CO_COLOR = '#D62728'   # red
FE_H2_COLOR = '#1F77B4'   # blue
FE_CO_LIGHT = '#FFCCCC'   # light red fill
FE_H2_LIGHT = '#CCE5FF'   # light blue fill


def setup_pub_axes(ax, xlabel='', ylabel=''):
    """Configure axes for publication style."""
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    # Show top and right spines (axis lines) but no ticks
    ax.spines['top'].set_visible(True)
    ax.spines['right'].set_visible(True)
    ax.tick_params(axis='x', top=False)
    ax.tick_params(axis='y', right=False)


def compute_time_hours(df, time_col='GC_Datetime'):
    """Compute elapsed wall-clock hours from the first GC sample (fallback)."""
    times = pd.to_datetime(df[time_col])
    t0 = times.min()
    return (times - t0).dt.total_seconds() / 3600.0


def compute_durability_only_hours(gc_datetimes, gamry_df):
    """
    Compute cumulative durability-only hours for each GC datetime.

    Sums actual data durations within each DTA file, excluding gaps
    between files (where break-in or other non-durability runs occur).

    For each GC datetime:
      Time_h = sum of durations of all prior DTA files
             + time elapsed within the current DTA file
    """
    gc_times = pd.to_datetime(gc_datetimes)

    # Build file segments: (start_datetime, end_datetime, duration_s) per DTA file
    segments = []
    for src_file, grp in gamry_df.groupby('Source File', sort=False):
        seg_start = grp['Absolute_Datetime'].min()
        seg_end = grp['Absolute_Datetime'].max()
        segments.append((seg_start, seg_end, src_file))

    # Sort segments by start time
    segments.sort(key=lambda x: x[0])

    # For each segment, compute its duration and cumulative prior duration
    seg_info = []
    cum_prior_s = 0.0
    for seg_start, seg_end, src_file in segments:
        dur_s = (seg_end - seg_start).total_seconds()
        seg_info.append((seg_start, seg_end, dur_s, cum_prior_s))
        cum_prior_s += dur_s

    # For each GC datetime, find which segment it falls in
    result_hours = []
    for t in gc_times:
        matched = False
        for seg_start, seg_end, dur_s, cum_prior_s in seg_info:
            if seg_start <= t <= seg_end:
                elapsed_in_seg = (t - seg_start).total_seconds()
                total_s = cum_prior_s + elapsed_in_seg
                result_hours.append(total_s / 3600.0)
                matched = True
                break
        if not matched:
            # GC time falls in a gap — find nearest prior segment end
            best_s = np.nan
            for seg_start, seg_end, dur_s, cum_prior_s in seg_info:
                if t > seg_end:
                    best_s = cum_prior_s + dur_s
                elif t < seg_start:
                    break
            result_hours.append(best_s / 3600.0 if np.isfinite(best_s) else np.nan)

    return result_hours


# ============================================================
# ========================= PLOTS ============================
# ============================================================

def plot_potential_vs_time(gamry_df, calc_df, output_dir):
    """Plot averaged and full potential vs time with decay rate."""
    plt.rcParams.update(PLOT_PARAMS)

    # Full data — compute durability-only hours for raw Gamry data
    full_hours = compute_durability_only_hours(gamry_df['Absolute_Datetime'], gamry_df)
    full_hours = pd.Series(full_hours, index=gamry_df.index)
    full_pot = gamry_df['Potential (V)']

    # Averaged data — use pre-computed durability-only Time_h
    avg_hours = calc_df['Time_h']
    avg_pot = calc_df['Gamry_Avg_Potential (V)']

    for data_type, hours, pot, suffix in [
        ('Full Data', full_hours, full_pot, 'full'),
        ('Averaged Data', avg_hours, avg_pot, 'avg')
    ]:
        fig, ax = plt.subplots(figsize=(7, 5))

        mask = np.isfinite(pot) & np.isfinite(hours)
        h_clean = hours[mask].values
        p_clean = pot[mask].values

        if data_type == 'Full Data':
            ax.plot(h_clean, p_clean, '-', color='#333333', linewidth=0.5, alpha=0.6)
        else:
            ax.plot(h_clean, p_clean, 'o', color='#333333',
                    markersize=MARKER_SIZE, markeredgewidth=MARKER_EDGE,
                    markeredgecolor='#333333', markerfacecolor='white',
                    linewidth=LINE_WIDTH)

        # Linear fit for decay rate
        if len(h_clean) > 10:
            slope, intercept, r_val, _, _ = stats.linregress(h_clean, p_clean)
            decay_uV_hr = slope * 1e6  # V/hr -> uV/hr
            fit_line = slope * h_clean + intercept
            ax.plot(h_clean, fit_line, '--', color=FE_CO_COLOR, linewidth=1.5)
            ax.text(0.05, 0.05,
                    f'Decay rate: {decay_uV_hr:.1f} uV/hr\n$R^2$ = {r_val**2:.4f}',
                    transform=ax.transAxes, fontsize=12, verticalalignment='bottom',
                    bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))

        setup_pub_axes(ax, 'Time (h)', 'Cell Potential (V)')
        ax.set_title(f'Durability — {data_type}')
        fname = os.path.join(output_dir, f'potential_vs_time_{suffix}.png')
        fig.savefig(fname)
        plt.close(fig)
        print(f"  Saved: {fname}")


def plot_fe_stacked(calc_df, output_dir, label='Chan1'):
    """Plot stacked FE_CO + FE_H2 vs time with shaded areas."""
    plt.rcParams.update(PLOT_PARAMS)

    hours = calc_df['Time_h']
    fe_co = calc_df['FE_CO_%'].values
    fe_h2 = calc_df['FE_H2_%'].values

    mask = np.isfinite(fe_co) & np.isfinite(fe_h2) & np.isfinite(hours)
    h = hours[mask].values
    co = fe_co[mask]
    h2 = fe_h2[mask]

    fig, ax = plt.subplots(figsize=(7, 5))

    # Shaded areas (stacked: H2 on bottom, CO on top)
    ax.fill_between(h, 0, h2, color=FE_H2_LIGHT, alpha=0.7, label='_nolegend_')
    ax.fill_between(h, h2, h2 + co, color=FE_CO_LIGHT, alpha=0.7, label='_nolegend_')

    # Lines and markers
    ax.plot(h, h2 + co, 'o-', color=FE_CO_COLOR, linewidth=LINE_WIDTH,
            markersize=MARKER_SIZE, markeredgewidth=MARKER_EDGE,
            markerfacecolor=FE_CO_COLOR, label='FE$_{CO}$ + FE$_{H_2}$')
    ax.plot(h, h2, 'o-', color=FE_H2_COLOR, linewidth=LINE_WIDTH,
            markersize=MARKER_SIZE, markeredgewidth=MARKER_EDGE,
            markerfacecolor=FE_H2_COLOR, label='FE$_{H_2}$')

    # Add text labels in the shaded regions
    mid_h = h[len(h)//2] if len(h) > 0 else 0
    mid_co = np.nanmean(co)
    mid_h2 = np.nanmean(h2)
    ax.text(mid_h, mid_h2 + mid_co/2, 'CO', fontsize=14, fontweight='bold',
            color=FE_CO_COLOR, ha='center', va='center', alpha=0.8)
    ax.text(mid_h, mid_h2/2, 'H$_2$', fontsize=14, fontweight='bold',
            color=FE_H2_COLOR, ha='center', va='center', alpha=0.8)

    ax.set_ylim(0, 130)
    ax.axhline(y=100, color='gray', linewidth=0.8, linestyle='--', alpha=0.5)
    setup_pub_axes(ax, 'Time (h)', 'Faradaic Efficiency (%)')
    ax.set_title(f'Faradaic Efficiency — {label}')
    ax.legend(loc='upper right')

    fname = os.path.join(output_dir, f'FE_stacked_{label}.png')
    fig.savefig(fname)
    plt.close(fig)
    print(f"  Saved: {fname}")


def plot_crossover_vs_time(calc_df, output_dir):
    """Plot crossover ratio vs time."""
    plt.rcParams.update(PLOT_PARAMS)

    hours = calc_df['Time_h']
    cr = calc_df['Crossover_Ratio'].values

    mask = np.isfinite(cr) & np.isfinite(hours)
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(hours[mask], cr[mask], 'o-', color='#2CA02C', linewidth=LINE_WIDTH,
            markersize=MARKER_SIZE, markeredgewidth=MARKER_EDGE,
            markerfacecolor='#2CA02C')
    setup_pub_axes(ax, 'Time (h)', 'CO$_2$ Crossover / O$_2$ Ratio')
    ax.set_title('CO$_2$ Crossover Ratio')

    fname = os.path.join(output_dir, 'crossover_ratio_vs_time.png')
    fig.savefig(fname)
    plt.close(fig)
    print(f"  Saved: {fname}")


def plot_spce_vs_time(calc_df, output_dir):
    """Plot SPCE vs time."""
    plt.rcParams.update(PLOT_PARAMS)

    hours = calc_df['Time_h']
    spce = calc_df['SPCE_%'].values

    mask = np.isfinite(spce) & np.isfinite(hours)
    fig, ax = plt.subplots(figsize=(7, 5))
    ax.plot(hours[mask], spce[mask], 'o-', color='#9467BD', linewidth=LINE_WIDTH,
            markersize=MARKER_SIZE, markeredgewidth=MARKER_EDGE,
            markerfacecolor='#9467BD')
    setup_pub_axes(ax, 'Time (h)', 'Single-Pass Conversion Efficiency (%)')
    ax.set_title('SPCE')

    fname = os.path.join(output_dir, 'SPCE_vs_time.png')
    fig.savefig(fname)
    plt.close(fig)
    print(f"  Saved: {fname}")


def plot_combined_2x2(calc_df, output_dir, label='Chan1'):
    """Combined 2x2 plot: potential, FE, SPCE, crossover."""
    plt.rcParams.update(PLOT_PARAMS)

    hours = calc_df['Time_h']

    fig, axes = plt.subplots(2, 2, figsize=(14, 10))
    fig.subplots_adjust(hspace=0.35, wspace=0.3)

    # (a) Averaged Potential
    ax = axes[0, 0]
    pot = calc_df['Gamry_Avg_Potential (V)'].values
    mask = np.isfinite(pot) & np.isfinite(hours)
    h_clean = hours[mask].values
    p_clean = pot[mask]
    ax.plot(h_clean, p_clean, 'o-', color='#333333', linewidth=LINE_WIDTH,
            markersize=MARKER_SIZE-2, markeredgewidth=MARKER_EDGE,
            markerfacecolor='white', markeredgecolor='#333333')
    if len(h_clean) > 10:
        slope, intercept, r_val, _, _ = stats.linregress(h_clean, p_clean)
        decay_uV_hr = slope * 1e6
        ax.plot(h_clean, slope * h_clean + intercept, '--', color=FE_CO_COLOR, linewidth=1.5)
        ax.text(0.05, 0.05, f'{decay_uV_hr:.1f} uV/hr',
                transform=ax.transAxes, fontsize=11, verticalalignment='bottom',
                bbox=dict(boxstyle='round', facecolor='white', alpha=0.8))
    setup_pub_axes(ax, 'Time (h)', 'Cell Potential (V)')
    ax.set_title('(a) Cell Potential', fontsize=14)

    # (b) Stacked FE
    ax = axes[0, 1]
    fe_co = calc_df['FE_CO_%'].values
    fe_h2 = calc_df['FE_H2_%'].values
    mask = np.isfinite(fe_co) & np.isfinite(fe_h2) & np.isfinite(hours)
    h = hours[mask].values
    co = fe_co[mask]
    h2_vals = fe_h2[mask]
    ax.fill_between(h, 0, h2_vals, color=FE_H2_LIGHT, alpha=0.7)
    ax.fill_between(h, h2_vals, h2_vals + co, color=FE_CO_LIGHT, alpha=0.7)
    ax.plot(h, h2_vals + co, 'o-', color=FE_CO_COLOR, linewidth=LINE_WIDTH,
            markersize=MARKER_SIZE-2, markeredgewidth=MARKER_EDGE,
            markerfacecolor=FE_CO_COLOR, label='FE$_{CO}$+FE$_{H_2}$')
    ax.plot(h, h2_vals, 'o-', color=FE_H2_COLOR, linewidth=LINE_WIDTH,
            markersize=MARKER_SIZE-2, markeredgewidth=MARKER_EDGE,
            markerfacecolor=FE_H2_COLOR, label='FE$_{H_2}$')
    ax.set_ylim(0, 130)
    ax.axhline(y=100, color='gray', linewidth=0.8, linestyle='--', alpha=0.5)
    setup_pub_axes(ax, 'Time (h)', 'Faradaic Efficiency (%)')
    ax.set_title(f'(b) FE — {label}', fontsize=14)
    ax.legend(loc='upper right', fontsize=10)

    # (c) SPCE
    ax = axes[1, 0]
    spce = calc_df['SPCE_%'].values
    mask = np.isfinite(spce) & np.isfinite(hours)
    ax.plot(hours[mask], spce[mask], 'o-', color='#9467BD', linewidth=LINE_WIDTH,
            markersize=MARKER_SIZE-2, markeredgewidth=MARKER_EDGE,
            markerfacecolor='#9467BD')
    setup_pub_axes(ax, 'Time (h)', 'SPCE (%)')
    ax.set_title('(c) Single-Pass Conversion', fontsize=14)

    # (d) Crossover
    ax = axes[1, 1]
    cr = calc_df['Crossover_Ratio'].values
    mask = np.isfinite(cr) & np.isfinite(hours)
    ax.plot(hours[mask], cr[mask], 'o-', color='#2CA02C', linewidth=LINE_WIDTH,
            markersize=MARKER_SIZE-2, markeredgewidth=MARKER_EDGE,
            markerfacecolor='#2CA02C')
    setup_pub_axes(ax, 'Time (h)', 'CO$_2$/O$_2$ Ratio')
    ax.set_title('(d) Crossover Ratio', fontsize=14)

    fname = os.path.join(output_dir, f'combined_2x2_{label}.png')
    fig.savefig(fname)
    plt.close(fig)
    print(f"  Saved: {fname}")


# ============================================================
# ================ METADATA AND FORMULAS =====================
# ============================================================

def build_metadata_df(cell_temp_avg=None, cell_temp_std=None):
    meta = [
        ('Parameter', 'Value', 'Unit', 'Notes'),
        ('Faraday Constant (F)', FARADAY_CONSTANT, 'C/mol', ''),
        ('Gas Constant (R)', GAS_CONSTANT, 'L*bar/(K*mol)', ''),
        ('STP Pressure (P_STP)', P_STP_BAR, 'bar', '101.325 kPa = 1 atm'),
        ('STP Temperature (T_STP)', T_STP_K, 'K', '0 deg C'),
        ('Surface Area', SURFACE_AREA_CM2, 'cm2', 'Cell active area'),
        ('', '', '', ''),
        ('Data Sources (per-row)', '', '', ''),
        ('Inlet CO2 Flow', 'LV_C_CO2_HI_FB_Mean', 'SLPM', 'From LabView (CO2 HI MFC)'),
        ('Outlet Flow', 'LV_outlet_flow_FB_Mean', 'SLPM', 'From LabView (outlet MFC, CO2-calibrated)'),
        ('CO mol% (Chan1)', 'Cathode_Carbon_Monoxide_Chan1', '%', 'From GC channel 1'),
        ('CO mol% (Chan2)', 'Cathode_Carbon_Monoxide_Chan2', '%', 'From GC channel 2'),
        ('H2 mol%', 'Cathode_Hydrogen_Chan1', '%', 'From GC'),
        ('Current Density', 'Gamry_Avg_Current Density (mA/cm2)', 'mA/cm2', 'Averaged over LV window'),
        ('', '', '', ''),
        ('MFC Correction Factors', '', '', 'Alicat viscosity-based at 25 deg C'),
        ('CF_CO2', CF_CO2, '', 'eta_CO2/eta_CO2 = 1.00'),
        ('CF_N2', CF_N2, '', 'eta_N2/eta_CO2 = 178.1/149.3'),
        ('CF_CO', CF_CO, '', 'eta_CO/eta_CO2 ~ 177/149.3'),
        ('CF_H2', CF_H2, '', 'eta_H2/eta_CO2 = 89.2/149.3'),
        ('CF_O2', CF_O2, '', 'eta_O2/eta_CO2 = 204.6/149.3 (used for hypothetical O2)'),
        ('', '', '', ''),
        ('Electrons per CO (n_e_CO)', N_ELECTRONS_CO, 'e-/molecule', 'CO2 + 2H+ + 2e- -> CO + H2O'),
        ('Electrons per H2 (n_e_H2)', N_ELECTRONS_H2, 'e-/molecule', '2H+ + 2e- -> H2'),
        ('', '', '', ''),
        ('Cell Temperature (avg)', cell_temp_avg, 'deg C', 'From LV_External_Watlow_1_FB (record only)'),
        ('Cell Temperature (std)', cell_temp_std, 'deg C', 'Std dev over durability test'),
        ('', '', '', ''),
        ('Reference', '', '', 'See Durability_GC_Matched_README.txt'),
        ('Alicat Manual', '', '', 'https://documents.alicat.com/manuals/old/Gas_Flow_Meter_Manual_rev6.pdf'),
        ('Alicat Correction', '', '', 'https://www.alicat.com/support/correcting-flow-data-after-choosing-the-wrong-gas-in-gas-select/'),
    ]
    return pd.DataFrame(meta[1:], columns=meta[0])


def build_formulas_df():
    formulas = [
        ('Column', 'Formula', 'Description'),
        ('Current_Density_A_cm2',
         'abs(Gamry_Avg_Current Density) / 1000',
         'Convert mA/cm2 to A/cm2'),
        ('Charge_per_min_C',
         'Current_Density_A_cm2 * Surface_Area * 60',
         'Total charge per minute (C/min)'),
        ('Corrected_Flow_SLPM',
         'Outlet_Flow / CF_mix; CF_mix = CF_CO*(CO%/100) + CF_H2*(H2%/100) + CF_CO2*((100-CO%-H2%)/100)',
         'Actual outlet flow corrected for gas mixture viscosity'),
        ('FE_CO_%',
         '(P_STP * Corrected_Flow) / (R * T_STP) * (CO%/100) * n_e_CO * F / Charge_per_min * 100',
         'Faradaic Efficiency for CO using ideal gas law at STP'),
        ('FE_H2_%',
         '(P_STP * Corrected_Flow) / (R * T_STP) * (H2%/100) * n_e_H2 * F / Charge_per_min * 100',
         'Faradaic Efficiency for H2'),
        ('FE_Total_%',
         'FE_CO + FE_H2',
         'Total Faradaic Efficiency'),
        ('FE_Mismatch_%',
         'FE_Total - 100',
         'Deviation from 100% FE; green <5%, yellow 5-10%, red >10%'),
        ('Hypothetical_O2_SLPM',
         '(Charge_per_min / (4*F)) * (R*T_STP/P_STP)',
         'Hypothetical O2 from Faraday\'s law. No MFC correction — theoretical value, not measured flow'),
        ('Utilized_CO2_SLPM',
         'Corrected_Flow * (CO%/100)',
         'CO2 consumed to produce CO (1:1 molar). H2 excluded (from HER)'),
        ('CO2_Outlet_SLPM',
         'Corrected_Flow * (100-CO%-H2%)/100',
         'Unreacted CO2 at cathode outlet'),
        ('CO2_Crossover_SLPM',
         'Inlet_Flow - (Utilized_CO2 + CO2_Outlet)',
         'CO2 lost to anode (carbon balance, H2 excluded)'),
        ('SPCE_%',
         '(Corrected_Flow * CO%/100) / Inlet_Flow * 100',
         'Single-Pass Conversion Efficiency'),
        ('Crossover_Ratio',
         'CO2_Crossover / Hypothetical_O2_SLPM',
         'CO2/O2 molar ratio (both in SLPM CO2 terms)'),
    ]
    return pd.DataFrame(formulas[1:], columns=formulas[0])


# ============================================================
# ========================== MAIN ============================
# ============================================================

def main():
    print("=" * 60)
    print("Durability-GC Matching + FE/SPCE/Crossover (v2)")
    print("=" * 60)

    # Step 1: Build timestamped Gamry timeline
    print("\n--- Step 1: Parsing DTA files ---")
    gamry_df = build_timestamped_timeline(DURABILITY_FOLDER)

    # Step 2: Read matched durability data
    print("\n--- Step 2: Reading Matched Durability from Combined Excel ---")
    matched_dur_df = pd.read_excel(COMBINED_EXCEL, sheet_name='Matched Durability')
    matched_dur_df['LV_Preceding_WindowStart'] = pd.to_datetime(matched_dur_df['LV_Preceding_WindowStart'])
    matched_dur_df['LV_Preceding_WindowEnd']   = pd.to_datetime(matched_dur_df['LV_Preceding_WindowEnd'])
    print(f"Matched Durability rows: {len(matched_dur_df)}")

    # Steps 3-4: Average and QC
    print("\n--- Step 3-4: Averaging Gamry data & QC checks ---")
    result_df = match_and_average(gamry_df, matched_dur_df)
    ok_count   = (result_df['Match_QC'] == 'OK').sum()
    fail_count = (result_df['Match_QC'] != 'OK').sum()
    print(f"Match results: {ok_count} OK, {fail_count} flagged")

    # Step 5: Compute FE, SPCE, Crossover — Channel 1
    print("\n--- Step 5: Computing FE, SPCE, Crossover ---")
    calc_df_ch1 = compute_fe_spce_crossover(result_df, gamry_df=gamry_df,
        co_col='Cathode_Carbon_Monoxide_Chan1', label='Chan1')

    # Step 5b: Channel 2
    calc_df_ch2 = compute_fe_spce_crossover(result_df, gamry_df=gamry_df,
        co_col='Cathode_Carbon_Monoxide_Chan2', label='Chan2')

    for label, cdf in [('Chan1', calc_df_ch1), ('Chan2', calc_df_ch2)]:
        if not cdf.empty:
            calc_ok   = (cdf['Calc_QC'] == 'OK').sum()
            calc_fail = (cdf['Calc_QC'] != 'OK').sum()
            print(f"  {label}: {calc_ok} OK, {calc_fail} flagged")
            for col in ['FE_CO_%', 'FE_H2_%', 'FE_Total_%', 'SPCE_%', 'Crossover_Ratio']:
                vals = cdf[col].dropna()
                if len(vals) > 0:
                    print(f"    {col}: mean={vals.mean():.2f}, min={vals.min():.2f}, max={vals.max():.2f}")

    # Step 5c: Cell temperature (metadata only)
    cell_temp_avg = None
    cell_temp_std = None
    temp_col = 'LV_External_Watlow_1_FB_Mean'
    if temp_col in result_df.columns:
        temp_vals = pd.to_numeric(result_df[temp_col], errors='coerce').dropna()
        if len(temp_vals) > 0:
            cell_temp_avg = round(temp_vals.mean(), 2)
            cell_temp_std = round(temp_vals.std(), 2)
            print(f"  Cell temperature: {cell_temp_avg} +/- {cell_temp_std} deg C")

    # Step 6: Export
    print("\n--- Step 6: Exporting ---")
    os.makedirs(OUTPUT_PLOT_DIR, exist_ok=True)

    with pd.ExcelWriter(OUTPUT_EXCEL, engine='openpyxl') as writer:
        result_df.to_excel(writer, sheet_name='Matched Results', index=False)
        if not calc_df_ch1.empty:
            calc_df_ch1.to_excel(writer, sheet_name='Calculated Results (Chan1)', index=False)
        if not calc_df_ch2.empty:
            calc_df_ch2.to_excel(writer, sheet_name='Calculated Results (Chan2)', index=False)
        build_formulas_df().to_excel(writer, sheet_name='Formulas', index=False)
        build_metadata_df(cell_temp_avg, cell_temp_std).to_excel(
            writer, sheet_name='Metadata', index=False)

    # Color-code QC and mismatch columns
    wb = load_workbook(OUTPUT_EXCEL)
    color_qc_column(wb['Matched Results'], 'Match_QC')
    for sheet_name in ['Calculated Results (Chan1)', 'Calculated Results (Chan2)']:
        if sheet_name in wb.sheetnames:
            color_qc_column(wb[sheet_name], 'Match_QC')
            color_qc_column(wb[sheet_name], 'Calc_QC')
            color_mismatch_column(wb[sheet_name])
    wb.save(OUTPUT_EXCEL)
    print(f"  Excel saved: {OUTPUT_EXCEL}")

    # Step 7: Plots
    print("\n--- Step 7: Generating plots ---")

    # Potential vs time (full + averaged)
    plot_potential_vs_time(gamry_df, calc_df_ch1, OUTPUT_PLOT_DIR)

    # Stacked FE plots for Chan1 and Chan2
    if not calc_df_ch1.empty:
        plot_fe_stacked(calc_df_ch1, OUTPUT_PLOT_DIR, label='Chan1')
    if not calc_df_ch2.empty:
        plot_fe_stacked(calc_df_ch2, OUTPUT_PLOT_DIR, label='Chan2')

    # Crossover and SPCE (using Chan1)
    if not calc_df_ch1.empty:
        plot_crossover_vs_time(calc_df_ch1, OUTPUT_PLOT_DIR)
        plot_spce_vs_time(calc_df_ch1, OUTPUT_PLOT_DIR)

    # Combined 2x2
    if not calc_df_ch1.empty:
        plot_combined_2x2(calc_df_ch1, OUTPUT_PLOT_DIR, label='Chan1')
    if not calc_df_ch2.empty:
        plot_combined_2x2(calc_df_ch2, OUTPUT_PLOT_DIR, label='Chan2')

    print("\nDone!")


if __name__ == "__main__":
    main()
