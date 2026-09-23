# Replication Package

**Paper**: Specialization or Fragmentation? On the Coupling of Agent Context Files and Project-Owned Skills  
**Dataset base**: [AIDev](https://huggingface.co/datasets/AIDev/AIDev) (publicly available via HuggingFace)

---

## Contents

```
replication/
├── README.md                        
│
├── Scripts/
│   ├── classify_aidev_repos.py        # Step 1 — Load AIDev and classify repos by ACF/SKILL.md presence
│   └── clone_repositories.py          # Step 2 — Full-clone the 297 project-owned SKILL.md repos
│
├── Dataset (RQ1)/
│   ├── discovered_repos.json          # All 2,807 AIDev repos with ACF/SKILL.md classification
│   ├── clone_summary.json             # Aggregate cloning statistics (see note below)
│   ├── skill_inventory_po.json        # Full inventory of 1,891 project-owned SKILL.md files
│   └── rq1_corrected.json             # RQ1 adoption rate per agent (corrected, project-owned only)
│
├── Dataset (RQ2 - Provenance)/
│   ├── provenance_453_files.csv       # 453-file sample with provenance labels (scratch/marketplace/from_acf/unknown)
│   ├── token_analysis.json            # Token comparison per repo (n=100), basis for +62.4% mean token overhead
│   ├── acf_tokens.json                # Per-repo ACF and SKILL.md token counts (basis for Fig. 4)
│   └── companion_dirs_scan.json       # Raw scan of companion directories (scripts/, references/, assets/)
│
├── Dataset (RQ3 - Content Migration)/
│   ├── content_migration.csv          # Per-skill ACF reference status at introduction and HEAD (61 from_acf skills)
│   └── companion_provenance.json      # Companion resource provenance agreement (137 directories)
│
└── Dataset (RQ4 - Evolution)/
    ├── evolution_summary_per_file.csv # Per-file evolution data (1,183 files with full commit history)
    ├── acf_coevolution.csv            # Per-file post-introduction ACF co-change (434 files, 103 repos)
    └── evolution_summary_stats.json   # Aggregate RQ4 statistics (modification rate, lifespan, per-agent)
```

---

## File Notes

### clone_summary.json
Reports aggregate statistics from the cloning stage. `full_clone_count: 297` reflects the number of repositories for which a full clone was attempted. `clone_status_counts.success: 213` reflects those that completed successfully; `clone_status_counts.failed: 84` reflects those that became unavailable before or during cloning.

The fields `skill_files_missing: 698` and `resolution_rate_pct: 63.1` reflect resolution against **all 1,891 expected files across all 297 repositories**, including the 84 that could not be cloned. The **1,193 files reported in the paper** come from the **211 repositories** where at least one SKILL.md file was confirmed present on disk after cloning (`repos_with_at_least_one_skill: 211`). The remaining 213 − 211 = 2 repositories were cloned successfully but had no SKILL.md files resolvable on disk at analysis time.

### provenance_453_files.csv — Provenance labels (RQ2)
Contains 453 rows (one per sampled SKILL.md file, up to three per repository). Key columns:
- `provenance_auto` — automated heuristic output (scratch/marketplace/marketplace_signal/from_acf/unknown)  
- `provenance_lana` — final label after manual review; this is the label used in the paper  
- `notes` — reason for any manual reclassification

The 175 files initially flagged as `marketplace_signal` were all manually reviewed; 83 were confirmed as `marketplace` and the remainder reclassified. Of the 113 `unknown` cases, 26 were resolved from commit context (23 scratch, 2 marketplace, 1 from_acf), leaving 88 as unknown in the final dataset.

### content_migration.csv — ACF reference status (RQ3)
Contains 61 rows (one per from_acf skill traced in RQ3). The file reflects **automated output before manual reclassification**. It shows `referenced_at_creation: 22` rather than the `21` reported in Table 3 of the paper. The difference is one skill (`antd`), whose ACF matches were incidental mentions of the Ant Design UI library rather than genuine skill invocations. The final reference-status counts (38 never-referenced / 21 at-creation / 2 added-later / 0 removed) are derived after that correction.

### token_analysis.json — Token comparison (RQ2)
Each row represents one repository. Fields `acf_tokens` and `mean_tokens_per_skill` use a word-count proxy (1.3× word count) as described in Section III-D. The mean token overhead of +62.4% (skill files are larger on average than the ACF) is computed across all 100 repositories in this file.

### evolution_summary_per_file.csv — Per-file evolution (RQ4)
Contains one row per file from the 1,183 files with resolvable commit history. The `modification_types` column records change types as a pipe-separated list (`content_update|minor_fix|...`). Files with `total_commits: 1` were not modified after introduction. The subset used for ACF co-evolution analysis (434 files across 103 repositories with a locatable ACF at the repository root) is in `acf_coevolution.csv`.

---

## Reproducing Key Numbers

| Paper claim | Source file | Field/column |
|---|---|---|
| 10.9% adoption (297/2,725 repos) | `rq1_corrected.json` | `adoption_rate_pct` |
| 1,891 project-owned files | `skill_inventory_po.json` | total entries |
| 1,193 files resolved on disk | `skill_inventory_po.json` | `exists_on_filesystem: true` |
| 211 repos with resolved files | `clone_summary.json` | `repos_with_at_least_one_skill` |
| 453-file RQ2 sample | `provenance_453_files.csv` | row count |
| scratch 47.0% / marketplace 19.6% | `provenance_453_files.csv` | `provenance_lana` distribution |
| +62.4% mean token overhead | `token_analysis.json` | `token_saving_pct` mean (negative = skill larger than ACF) |
| 61 from_acf skills (RQ3) | `content_migration.csv` | row count |
| 62.3% never referenced | `content_migration.csv` | `reference_category = never_referenced` |
| 137 companion directories | `companion_provenance.json` | total entries |
| 54.3% modified at least once | `evolution_summary_stats.json` | `files_modified_at_least_once / total_files_analyzed` |
| 16.0% ACF co-change rate | `acf_coevolution.csv` | `n_cochange_commits / n_post_intro_commits` (aggregate) |
| 434 files / 103 repos (RQ4 ACF) | `acf_coevolution.csv` | row count / distinct `repo_key` |
