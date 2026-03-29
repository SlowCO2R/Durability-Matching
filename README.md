# Durability-Matching
Match Durability and GC+LabView
=====================================================

Anode Setup
-----------
- Anolyte: 0.1 M KHCO3, liquid recirculation loop at 40 mL/min
- No N2 carrier gas on anode side
- Anode gas evolves from the anolyte (O2 from OER + degassed CO2)

CO2 Crossover: Two Methods
--------------------------
1. Cathode carbon balance (from cathode outlet MFC):
     CO2_crossover = Inlet_CO2 - (Utilized_CO2 + CO2_at_cathode_outlet)
   This captures the TOTAL CO2 leaving the cathode side, regardless of
   whether it remains dissolved in the anolyte or evolves as gas.
   Typical values: ~0.05-0.06 SLPM, CO2/O2 ratio ~3.0-3.7

2. Anode GC direct (from anode gas sampling):
     Measures CO2 mol% in the gas that evolves from the anolyte.
   This captures only the GASEOUS CO2 that degasses from the liquid.
   Typical values: ~0.001-0.002 SLPM, CO2/O2 ratio ~0.04-0.14

Why the ~30x Discrepancy?
-------------------------
CO2 that crosses the membrane as carbonate/bicarbonate enters the 0.1 M
KHCO3 anolyte. Most of it stays dissolved rather than evolving as gas:

  CO2(g) + H2O <-> H2CO3 <-> H+ + HCO3- <-> 2H+ + CO3^2-

In alkaline conditions (anolyte pH stabilizes around ~9 due to carbonate
accumulation), the equilibrium strongly favors dissolved HCO3-/CO3^2-.
Only a small fraction escapes as gaseous CO2. This is consistent with
the observed pH swing: fresh 0.1 M KHCO3 starts near pH 8.3, and the
pH rises to ~9 as carbonate accumulates from crossover.

Interpretation
--------------
- Cathode carbon balance = total CO2 crossover (gaseous + dissolved)
- Anode GC = gaseous CO2 crossover only (what degasses from anolyte)
- Difference = CO2 accumulating in anolyte as dissolved HCO3-/CO3^2-

Both values are useful:
  - Cathode method: total membrane crossover rate (for membrane comparison)
  - Anode method: gaseous CO2 loss (relevant to gas-phase product purity)
  - Difference: rate of anolyte carbonate buildup (anolyte lifetime)

Anode Gas Calculation (v3)
--------------------------
Since there is no N2 carrier gas, the anode evolved gas is assumed to be
O2 + CO2 only. The calculation uses theoretical O2 from Faraday's law as
the reference:

  O2_theoretical = (I * A * 60) / (4 * F) * (R * T_STP / P_STP)  [SLPM]
  O2_fraction = (100% - CO2% - CO% - H2%) / 100
  Total_anode_gas = O2_theoretical / O2_fraction
  CO2_crossover_gas = Total_anode_gas * (CO2% / 100)
  CO2/O2_ratio_anode = CO2_crossover_gas / O2_theoretical

This only captures gaseous CO2. The total crossover is better estimated
from the cathode carbon balance.

Theoretical CO2/O2 Ratio at Anode (from literature)
----------------------------------------------------
The CO2/O2 molar ratio in anode gas depends on the dominant charge
carrier through the AEM:

  Charge carrier   Anode reaction                         CO2/O2
  ---------------  -------------------------------------  ------
  HCO3-            4HCO3- -> 4CO2 + 2H2O + O2 + 4e-     4
  CO3^2-           2CO3^2- -> 2CO2 + O2 + 4e-            2
  OH-              4OH- -> 2H2O + O2 + 4e-               0

Our cathode carbon balance gives CO2/O2 ~3.3, between HCO3- (4) and
CO3^2- (2), suggesting a mixture of both as charge carriers.

Our anode GC gives CO2/O2 ~0.04, much lower because most crossed-over
CO2 stays dissolved in the 0.1 M KHCO3 anolyte (pH ~9) as HCO3-/CO3^2-.

Literature reports CO2/O2 ~2 at anode for KOH anolyte systems where CO2
evolves freely as gas, confirming CO3^2- as dominant carrier. The J. Phys.
Chem. C paper (Endrodi et al.) measured CO2/O2 decreasing from ~3 to ~2
at 200 mA/cm2, indicating a shift from HCO3- to CO3^2- over time.

References (CO2 crossover / carbonate transport)
-------------------------------------------------
[1] Endrodi et al., "Carbonate Ion Crossover in Zero-Gap, KOH Anolyte
    CO2 Electrolysis," J. Phys. Chem. C, 2022.
    DOI: 10.1021/acs.jpcc.1c08430
    — Measured CO2/O2 ratio ~3 to ~2 at 200 mA/cm2. Shift indicates
      transition from HCO3- to CO3^2- as dominant charge carrier.

[2] Blommaert et al., "Carbonation in Low-Temperature CO2 Electrolyzers:
    Causes, Consequences, and Solutions," Ind. Eng. Chem. Res., 2023.
    DOI: 10.1021/acs.iecr.3c00118
    — Comprehensive review. Up to 95% CO2 loss in alkaline systems.
      Discusses carbon balance, dissolved vs gaseous CO2.

[3] Kutz et al., "Carbon Dioxide and Water Electrolysis Using New Alkaline
    Stable Anion Membranes," Front. Chem., 2018.
    DOI: 10.3389/fchem.2018.00263
    — Derives theoretical CO2/O2 = 4 (HCO3-), 2 (CO3^2-), 0 (OH-).

[4] Lim et al., "Membrane Electrode Assembly Design to Prevent CO2
    Crossover in CO2 Reduction Reaction Electrolysis," Commun. Chem., 2022.
    DOI: 10.1038/s42004-022-00806-0
    — Membrane strategies to reduce crossover.

[5] Reyes et al., "CO2 Loss into Solution: An Experimental Investigation
    of CO2 Electrolysis with a Membrane Electrode Assembly Cell,"
    ACS Appl. Energy Mater., 2024.
    DOI: 10.1021/acsaem.4c01101
    — Directly investigates dissolved vs gaseous CO2 loss pathway.

TODO: Review these references by 2026-04-06 (next week).

Configuration
-------------
- Anolyte recirculation: 40 mL/min, 0.1 M KHCO3
- Cell area: 25 cm2
- No N2 carrier gas on anode
- GC anode injection pattern: 1 anode per 2 cathode injections
- Anode classification: CO% + H2% < 1% threshold
