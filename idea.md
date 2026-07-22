# TUI GamePigeon

## What it is

A terminal arcade for iMessage-style quick games while a coding agent is working. Launch it from Codex or Claude Code, play for a few minutes, then return to work without losing the game.

## MVP loop

1. Open Arcade from the coding harness.
2. Pick 2048, Tetris, Minesweeper, or any other cart in the menu.
3. Play in the terminal.
4. Save the current game and every accepted action locally.
5. Reopen the same game later or replay the recorded run.

## Current state (July 16)

- Seven games are playable: 2048, Tetris, Snake, Minesweeper, Sokoban, Connect Four, and Sudoku — all seeded and traced through the same gym-style contract.
- The game menu and shared game registry are implemented.
- Codex, Claude Code, and OpenCode adapters launch Arcade in a persistent native HTML overlay.
- Each run writes a seeded JSONL trajectory to `~/.tui-gamepigeon/trajectories/`.
- Product analytics are optional and become no-ops without a PostHog key.

## Next priority: save and replay

The trajectory log already records the seed and accepted actions. Finish the user-facing loop around it:

- persist enough metadata to identify an active or completed run;
- restore an unfinished game from its seed and recorded actions;
- replay a completed run deterministically;
- expose resume/replay through the existing menu or launcher;
- verify save, restore, and replay for every game.

State persistence and trajectory logging are the same local feature for the MVP. Do not build a second storage system.

## After the MVP

- OAuth identity and cross-device sync
- upload consent and a remote trajectory store
- policy training, leaderboards, and multiplayer
- Linux native overlay packaging

These are intentionally deferred until install, play, save, resume, and replay work reliably.

## Working rules

- Track work in GitHub Issues and coordinate before starting overlapping work.
- Use pull requests; include a short human-written summary and a runnable check.
- Require an account on first plugin launch; keep gameplay and local writes resilient after sign-in when analytics or upload services are unavailable.
