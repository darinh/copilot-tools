# Subproject conventions

Not rendered. `render_subproject()` replaces this file entirely with the resolved facts for one
subproject; nothing written here ships. It exists so the shape is reviewable in the repository
rather than only in a Python string.

The one rule this file obeys, and the reason it is this short:

**A subproject file adds facts. It never restates a rule the root file already carries, and it
never contradicts one.** Claude Code concatenates the parent and the child; Codex lets the nearer
file win (FR-9). Only additive content behaves the same under both, so a rule written in both
places is a rule that means two things depending on which harness is reading. Repeating even an
*identical* sentence is not safe: the copies drift, and the one that is wrong is whichever was
regenerated last, which is visible from neither file.

What legitimately belongs here is what the root file cannot know — which subproject this is, the
paths it owns, and the contracts it shares. Those are resolved values, and a value cannot
contradict a rule.
