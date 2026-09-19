"""Named sparring opponents: (bot directory under bots/, env overrides, style tags).

Every opponent is built from one of our own controllers (so its fire control is at least as
good as ours) with a different plan: this overstates their micro and cannot reproduce every
real opponent (see README).  Tags group them by the failure type they probe.
"""

V1 = "styles_v1"    # v1 core (formation around the payload)
V5 = "styles_v5"    # v5 core (fire control + firing-position planner)
V61 = "styles_v61"  # v6.1 core (press at 6 tiles)
V9 = "styles_v9"    # v9 core (production fire control and healing)

ROSTER = {
    # ---------------------------------------------------------------- history
    "codex": ("codex", "", {"history"}),
    "v61": ("v61", "", {"history"}),
    "v7": ("v7", "", {"history"}),
    "v12": ("baseline_v12", "", {"history", "baseline"}),
    # ------------------------------------------------------------------ rush
    "rushB": (V1, "OPP=advanced,IAMABOT_OPENING=BBBBBBBBBBBBBBBBB,IAMABOT_EXTRACTORS=0,IAMABOT_HEALER_RATIO=0", {"rush", "no_miners"}),
    "rushB5": (V5, "OPP=advanced,IAMABOT_OPENING=BBBBBBBBBBBBBBBBB,IAMABOT_EXTRACTORS=0,IAMABOT_HEALER_RATIO=0", {"rush", "no_miners"}),
    "g3_rush": (V61, "OPP=advanced,IAMABOT_OPENING=BBBBBBBBBBBBBBBBB,IAMABOT_EXTRACTORS=0,IAMABOT_HEALER_RATIO=0", {"rush", "no_miners"}),
    "g3_rushH": (V61, "OPP=advanced,IAMABOT_OPENING=BBHBBHBBHBBHBBHBB,IAMABOT_EXTRACTORS=0,IAMABOT_HEALER_RATIO=0.33", {"rush", "no_miners", "heal"}),
    "clanker": (V61, "OPP=advanced,IAMABOT_OPENING=BBBBBHBEEBBBBHBBB,IAMABOT_EXTRACTORS=3,IAMABOT_HEALER_RATIO=0.08,IAMABOT_D_PRESS=5.5", {"rush", "real:clankerbot"}),
    # -------------------------------------------------------------- economy
    "greedy": (V1, "OPP=advanced,IAMABOT_OPENING=EEEEEEEEBBBBHBBBB,IAMABOT_EXTRACTORS=8", {"econ"}),
    "greedy5": (V5, "OPP=greedy", {"econ"}),
    "g3_econ8": (V61, "OPP=advanced,IAMABOT_OPENING=EEEEEEEEBBHBBHBBH,IAMABOT_EXTRACTORS=8", {"econ"}),
    "g3_econ5": (V61, "OPP=advanced,IAMABOT_OPENING=EEEEEBBHBBHBBHBBH,IAMABOT_EXTRACTORS=6", {"econ"}),
    "st24": (V5, "OPP=st24", {"econ", "harass", "real:SyntaxTerror"}),
    "g3_st": (V61, "OPP=st24", {"econ", "harass", "real:SyntaxTerror"}),
    "dibsfa8": (V61, "OPP=dibsfa", {"econ", "real:DIBSFA"}),
    "dibsfa_turtle": (V61, "OPP=dibsfa_turtle", {"econ", "turtle", "real:DIBSFA"}),
    # ---------------------------------------------------------------- heal
    "healheavy": (V1, "OPP=advanced,IAMABOT_OPENING=BHBHBHBHBHBHBHBHB,IAMABOT_HEALER_RATIO=0.45", {"heal"}),
    "heal5": (V5, "OPP=advanced,IAMABOT_OPENING=BHBHBHBHBHBHBHBHB,IAMABOT_HEALER_RATIO=0.45", {"heal"}),
    "g3_heal45": (V61, "OPP=advanced,IAMABOT_OPENING=EBHBHEBHBHEBHBHBB,IAMABOT_HEALER_RATIO=0.45", {"heal"}),
    "noey": (V61, "OPP=noey", {"heal", "real:noeyedeer"}),
    # ---------------------------------------------- Gang-style deposit raids
    "gang2": (V5, "OPP=gang2", {"raid", "real:Gang"}),
    "gangx": (V61, "OPP=gangx", {"raid", "real:Gang"}),
    "gangx5k": (V61, "OPP=gangx,GANG_UNTIL=5000", {"raid", "real:Gang"}),
    "gang7": (V61, "OPP=gangx,GANG_OPENING=BBBBBBBHEEEEEEEEB,GANG_UNTIL=1500,IAMABOT_D_PRESS=7.5", {"raid", "real:Gang"}),
    "thief": (V1, "OPP=thief", {"raid"}),
    "thief5": (V5, "OPP=thief", {"raid"}),
    "g3_thief": (V61, "OPP=thief", {"raid"}),
    "raider": (V1, "OPP=raider", {"harass"}),
    # ------------------------------------------ Janice-style payload cover
    "jkt": (V61, "OPP=advanced,IAMABOT_OPENING=BBBHBHBEEBHBBHBBB,IAMABOT_EXTRACTORS=3,IAMABOT_HEALER_RATIO=0.35,IAMABOT_D_PRESS=7.2", {"cover", "heal", "real:JaniceKeepTalking"}),
    "jkt_escort": (V61, "OPP=escort", {"cover", "real:JaniceKeepTalking"}),
    "jkt_cover": (V61, "OPP=cover", {"cover", "real:JaniceKeepTalking"}),
    "jkt_cover6": (V61, "OPP=cover,COVER_R=6.0", {"cover"}),
    "jkt_cover35": (V61, "OPP=cover,COVER_R=3.5", {"cover"}),
    # ------------------- payload deathball (Team Name / JKT / clankerbot, 2026-09-19)
    # The whole army stacked on the capture circle, pushing from the first fight on.
    "ball_tn": (V61, "OPP=escort,IAMABOT_OPENING=BEHEBBEBHBBBHBBBH,ESCORT_RING=1.8,ESCORT_GAP=1.0", {"ball", "cover", "real:Team Name"}),
    "ball_jkt": (V61, "OPP=escort,IAMABOT_OPENING=EBBBHBHBEEBHBBHBB,ESCORT_RING=1.8,ESCORT_GAP=0.9", {"ball", "cover", "heal", "real:JaniceKeepTalking"}),
    "ball_clank": (V61, "OPP=escort,IAMABOT_OPENING=EBBBBBHBEEHHBHBHB,ESCORT_RING=2.2,ESCORT_GAP=1.2,IAMABOT_HEALER_RATIO=0.4", {"ball", "heal", "real:clankerbot"}),
    "db_jkt": (V9, "OPP=deathball", {"ball", "heal", "real:JaniceKeepTalking"}),
    "db_tn": (V9, "OPP=deathball,BALL_OPENING=BEHEBBEBHBBBHBBBH,BALL_EXTRACTORS=6", {"ball", "real:Team Name"}),
    "db_clank": (V9, "OPP=deathball,BALL_OPENING=EBBBBBHBEEHHBHBHB,BALL_HEALERS=0.4", {"ball", "heal", "real:clankerbot"}),
    "db_clank_behind": (V9, "OPP=deathball,BALL_OPENING=EBBBBBHBEEHHBHBHB,BALL_HEALERS=0.4,BALL_SIDE=-1,BALL_R_IN=1.4,BALL_R=4.5", {"ball", "heal", "cover", "real:clankerbot"}),
    "db_tn_behind": (V9, "OPP=deathball,BALL_OPENING=BEHEBBEBHBBBHBBBH,BALL_EXTRACTORS=6,BALL_SIDE=-1,BALL_R_IN=1.4,BALL_R=4.5", {"ball", "cover", "real:Team Name"}),
    # ------------------------------------------------ turtle / stall / hug
    "reference": ("reference", "", {"turtle", "stack"}),
    "stacker": (V1, "OPP=stacker", {"turtle", "stack"}),
    "sniper": (V1, "OPP=sniper", {"turtle"}),
    "camper5": (V5, "OPP=camper", {"turtle"}),
    "g3_camper": (V61, "OPP=camper", {"turtle"}),
    "terry7": (V5, "OPP=terry7", {"turtle", "stack", "real:terryduan-chn"}),
    # --------------------------------------------------------------- chase
    "hunter": (V1, "OPP=hunter", {"chase"}),
    "hunter5": (V5, "OPP=hunter", {"chase"}),
    # ------------------------------------------------ distance variants
    "g3_d5": (V61, "OPP=advanced,IAMABOT_D_PRESS=5.0", {"distance"}),
    "g3_d7": (V61, "OPP=advanced,IAMABOT_D_PRESS=7.5", {"distance"}),
}

# The quick regression set: one or two per failure type, including every real-opponent
# replica.  Run it after every single tactical change; run the full roster before release.
QUICK = [
    "v12", "v7", "jkt", "jkt_cover", "gang7", "gangx", "noey", "clanker",
    "st24", "dibsfa8", "dibsfa_turtle", "stacker", "g3_heal45",
]

# The key set (about a minute with -j 10): the replicas of the teams that beat us on the
# server, plus the ones where past changes broke first.  Use it for every experiment;
# run `full` only before a release.
KEY = ["clanker", "jkt", "db_clank", "dibsfa8", "st24", "noey", "v12"]
