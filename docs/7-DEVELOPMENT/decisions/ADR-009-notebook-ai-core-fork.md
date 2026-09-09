# ADR-009: Notebook + AI-organizing core fork (media removed, async transformations)

- **Status**: Accepted (local fork, not intended for upstream)
- **Date**: 2026-09
- **Related**: branch `wip/async-transformation-execute`; [ADR-002](ADR-002-external-libraries.md) (media delegated to external libs); the planned "DeepSeek Harness" plugin experiment

## Context

This fork is used as a personal notebook + AI-organizing tool only: import sources, embed, summarize/transform, notes, chat and ask. Podcast, YouTube/audio ingestion and TTS/STT add meaningful image weight (~0.4 GB of a 2.47 GB image) and UI/API surface that is never used. Upstream ships these as baked-in features of a NotebookLM-style monolith; that is also the seam a future "DeepSeek Harness" plugin experiment wants to target (features should be optional/installable, not mandatory).

## Decision

On this fork:

- **Remove Podcast end-to-end** — backend (`api/podcast_service.py`, routers `podcasts`/`episode_profiles`/`speaker_profiles`, `commands/podcast_commands.py`, `open_notebook/podcasts/`), frontend (page, `components/podcasts/*`, api/hooks/types), registration in `api/main.py`, `commands/__init__.py`, `api/command_service.py`, `open_notebook/config.py` (`PODCASTS_FOLDER`), and the `podcast-creator` dependency (drops moviepy/imageio-ffmpeg/pydub).
- **Block TTS/STT instead of deep-removing them** — the modalities are not advertised (`/api/models/providers` supported types, model discovery) and cannot be assigned (model create rejects them; default-model PUT rejects `default_text_to_speech_model`/`default_speech_to_text_model`; auto-assign already never filled them). The settings UI no longer shows TTS/STT default rows. The underlying multi-modality provider/credential code stays dormant.
- **Drop the YouTube dependency path** — the Docker build prunes `pytubefix*` and `nodejs_wheel*` from the venv right after `uv sync` (saves the ~205 MB embedded Node runtime). Safe because content-core imports `pytubefix` only lazily inside its YouTube processor; there was no dedicated YouTube source type in the UI.
- **Transformation execution is async** — `POST /api/transformations/execute-async` returns `202` + a `job_id` immediately, `GET /api/transformations/jobs/{job_id}` is polled until `done/error`; the Playground shows a live "Running… (Ns)" counter. The old synchronous `execute` stays for compatibility.
- **Clean the i18n fallout** — the 239 orphaned podcast/TTS-STT keys were removed from all 14 locales and `interpolation.test.ts` updated, keeping the repo's unused-key and parity invariants green.

## Alternatives considered

- **Hide podcast instead of removing** — rejected: dead code and deps cost real image/UX weight for no benefit on a personal fork.
- **Deep-remove stt/tts model types** — rejected: it would touch esperanto model classes, credential config and provider discovery broadly for zero runtime win; blocking + hiding is enough while keeping dormant code recoverable.
- **Keep pytubefix and only strip nodejs_wheel** — rejected after confirming content-core's lazy import makes dropping both safe; user does not need YouTube.
- **Keep orphan i18n keys and relax the unused-key test** — rejected in favor of cleaning all locales, preserving the upstream invariant so diffs stay reviewable.

## Consequences

- Easier: image shrinks (~2.07 GB vs 2.47 GB), sidebar/API surface is notebook+AI only, no podcast worker commands, Playground transformations no longer hang silently on one long HTTP call.
- Watch:
  - Pre-existing podcast/episode/speaker/TTS-STT rows and tables in SurrealDB are harmless dead data (no migration written).
  - The async-job store is in-memory (single-worker assumption): an API restart drops running jobs and the frontend surfaces "Job not found" — an acceptable degradation, not silent.
  - Importing a YouTube URL now fails at processing time (pytubefix removed); non-YouTube content is unaffected.
  - Dormant multi-modality code still parses/decrypts provider config but is never exposed in UI or APIs.
- Reference for the plugin experiment: keep feature seams (router groups, commands registry, domain packages, `supported_types`) opt-in rather than baked in — exactly what ADR-002 already argues for media, and what this fork demonstrates is a clean, lean "notebook + AI" core to target.
