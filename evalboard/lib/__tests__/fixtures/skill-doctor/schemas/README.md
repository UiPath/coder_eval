# skill-doctor contract v1 schemas

Copies of the producer's JSON Schemas (`src/skill_doctor/schemas/` in the
skill-doctor repo), taken 2026-11-20. `doctor-schema.test.ts` checks that every
field `lib/doctor.ts` reads is declared here and that the hand-written fixtures
use only declared keys, so a producer-side rename fails a test once these are
refreshed. Refresh them whenever the doctor's contract changes.

fixes_v1 pins only `id`, `task_id` and `outcome` of a candidate; the other
candidate fields the board reads (`covers`, `patch_lines`, `title`, `gates`)
are not pinned by the producer yet.
