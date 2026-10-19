# Update notes

## Unreleased — experimental public-information toolkit

- Add an opt-in v10 toolkit experiment independently of PR #7's desktop testing
  branch: personalized headlines across nine categories, city lookup, weather,
  air quality, dated currency reference rates, holidays, recent earthquakes,
  Wikipedia background and book discovery.
- Add explicitly named local feed profiles with followed/excluded terms, strict
  matching, partial updates and forgetting. No inferred speaker identity or cloud
  profile synchronization. Public headline requests contain no interest/profile data.
- Preserve existing tool declarations and add v10 tool-response routing; keep the
  base conversation/model/audio pipeline and deployment settings unchanged.
- Add bounded HTTPS reads, short caches, partial-source failure handling, container
  source inclusion, offline tests and explicit synthetic provider smoke checks.
- Windows evidence: 44 offline checks (4 simulated core integration, 40 toolkit)
  and 17 public-provider smoke checks pass. Live Gemini/hardware, interruption and
  reconnect behavior, Docker image build, commercial terms and lead approval remain
  pending. See Baymax-main/toolkit/EXPERIMENTAL.md.
