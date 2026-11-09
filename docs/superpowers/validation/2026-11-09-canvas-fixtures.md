# Canvas export validation: Instructure test fixtures

Run with `scripts/validate_exports.cjs` against the 32 Canvas/Common Cartridge fixture archives in
`instructure/canvas-lms` `spec/fixtures/migration` (not vendored here; Canvas is AGPL). Counts only; no course content.

| Export | Result | Export MB | Slim MB | Pages | Assign. | Quizzes (questions) | Banks (questions) | Discussions | Modules | Rubrics | Outcomes | Weighted | Syllabus | Media | Student details removed | Warnings |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| asmnt_example.zip | ✅ | 0.002 | 0.002 | 1 | 1 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | no | 0 | 0 | 1: Module metadata preserved; downstream module navigation may  |
| canvas_announcement.zip | ✅ | 0.005 | 0.005 | 0 | 0 | 0 (0) | 0 (0) | 2 | 1 | 0 | 0 | no | yes | 0 | 0 | 1: Module metadata preserved; downstream module navigation may  |
| canvas_attachment.zip | ✅ | 0.135 | 0.003 | 0 | 0 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | no | 1 | 0 | 2: Module metadata preserved; downstream module navigation may ; No supported instructional content was found in this cartrid |
| canvas_cc_only_questions.zip | ✅ | 0.013 | 0.009 | 0 | 0 | 0 (0) | 1 (41) | 0 | 1 | 0 | 0 | no | yes | 0 | 0 | 1: Module metadata preserved; downstream module navigation may  |
| canvas_cc_utf16_error.zip | ✅ | 0.003 | 0.001 | 0 | 0 | 0 (0) | 0 (0) | 1 | 0 | 0 | 0 | no | yes | 0 | 0 | 1: Missing archive reference |
| canvas_matching_reorder.zip | ✅ | 0.072 | 0.004 | 0 | 0 | 1 (3) | 0 (0) | 0 | 1 | 0 | 0 | no | no | 2 | 0 | 2: Module metadata preserved; downstream module navigation may ; Partial quiz coverage (m24122cb13c7a474c1976c9706ae36521d_as |
| canvas_quiz_media_comment.zip | ✅ | 0.036 | 0.011 | 0 | 0 | 2 (2) | 1 (4) | 0 | 1 | 0 | 0 | no | yes | 3 | 0 | 3: Module metadata preserved; downstream module navigation may ; Partial quiz coverage (i114d5d49b9705a5cce2878e42fd5b022); Partial quiz coverage (i904784586a492943635adcce916b8360) |
| cc_ark_test.zip | ✅ | 0.091 | 0.014 | 3 | 2 | 2 (0) | 0 (0) | 4 | 2 | 0 | 2 | no | yes | 2 | 0 | 8: External LTI/web link resource not imported; Module metadata preserved; downstream module navigation may ; Module reference has no imported content; Partial quiz coverage (i11ae878a000f370b007a8fe081d0ded9); Partial quiz coverage (i2da11ea691f704f9b32ed3fa563af30e); Quiz missing usable question text |
| cc_assignment_extension.zip | ✅ | 0.027 | 0.006 | 0 | 3 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | yes | 1 | 0 | 2: Missing archive reference; Module metadata preserved; downstream module navigation may  |
| cc_default_qb_test.zip | ✅ | 0.007 | 0.003 | 0 | 0 | 0 (0) | 1 (1) | 0 | 1 | 0 | 0 | no | yes | 0 | 0 | 1: Module metadata preserved; downstream module navigation may  |
| cc_dotdot_madness.zip | ✅ | 0.002 | 0.002 | 1 | 0 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | no | 1 | 0 | 1: Module metadata preserved; downstream module navigation may  |
| cc_empty_link.zip | ✅ | 0.002 | 0.001 | 1 | 0 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | no | 1 | 0 | 1: Module metadata preserved; downstream module navigation may  |
| cc_file_to_page_test.zip | ✅ | 0.010 | 0.001 | 0 | 0 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | no | 1 | 0 | 3: Module metadata preserved; downstream module navigation may ; Module reference has no imported content; No supported instructional content was found in this cartrid |
| cc_full_test.zip | ✅ | 0.025 | 0.014 | 2 | 0 | 2 (22) | 1 (11) | 2 | 1 | 0 | 0 | no | no | 8 | 0 | 17: External LTI/web link resource not imported; Missing archive reference; Module metadata preserved; downstream module navigation may ; Module reference has no imported content; Partial quiz coverage (I_00003_R); Partial quiz coverage (I_00004_R); Resource has no usable bundled files |
| cc_full_test_smaller.zip | ❌ slimming: This ZIP is not a Canvas course export: imsmanifest.xml is missing. | | | | | | | | | | | | | | | |
| cc_inline_qti.zip | ✅ | 0.009 | 0.009 | 0 | 0 | 1 (14) | 1 (14) | 0 | 1 | 0 | 0 | no | no | 0 | 0 | 4: Module metadata preserved; downstream module navigation may ; Module reference has no imported content; Partial quiz coverage (I_00004_R); Resource has no usable bundled files |
| cc_lti_combine_test.zip | ✅ | 0.005 | 0.003 | 0 | 0 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | no | 0 | 0 | 12: External LTI/web link resource not imported; Module metadata preserved; downstream module navigation may ; Module reference has no imported content; No supported instructional content was found in this cartrid |
| cc_nested.zip | ✅ | 0.002 | 0.002 | 1 | 1 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | no | 0 | 0 | 1: Module metadata preserved; downstream module navigation may  |
| cc_outcomes.imscc | ✅ | 0.004 | 0.003 | 0 | 0 | 0 (0) | 0 (0) | 0 | 1 | 0 | 4 | no | no | 0 | 0 | 2: Module metadata preserved; downstream module navigation may ; No supported instructional content was found in this cartrid |
| cc_pattern_match.zip | ✅ | 0.018 | 0.012 | 1 | 0 | 2 (22) | 1 (11) | 1 | 1 | 0 | 0 | no | no | 2 | 0 | 6: External LTI/web link resource not imported; Module metadata preserved; downstream module navigation may ; Module reference has no imported content; Partial quiz coverage (I_00003_R); Partial quiz coverage (I_00004_R); Resource has no usable bundled files |
| cc_syllabus.zip | ✅ | 0.001 | 0.001 | 0 | 0 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | yes | 0 | 0 | 1: Module metadata preserved; downstream module navigation may  |
| cc_unsupported_resources.zip | ❌ import: SlimImportError: Unsafe or malformed XML: imsmanifest.xml | | | | | | | | | | | | | | | |
| exported_data_cm.zip | ❌ slimming: This ZIP is not a Canvas course export: imsmanifest.xml is missing. | | | | | | | | | | | | | | | |
| file.zip | ❌ slimming: This ZIP is not a Canvas course export: imsmanifest.xml is missing. | | | | | | | | | | | | | | | |
| macfile.zip | ❌ slimming: This ZIP is not a Canvas course export: imsmanifest.xml is missing. | | | | | | | | | | | | | | | |
| media_quiz_qti.zip | ✅ | 0.065 | 0.002 | 0 | 0 | 1 (1) | 0 (0) | 0 | 0 | 0 | 0 | no | no | 1 | 0 | 1: Partial quiz coverage (ibd704bbef98a99aca064aa58277e1caf) |
| page-with-media.imscc | ✅ | 0.069 | 0.003 | 1 | 0 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | yes | 1 | 0 | 1: Module metadata preserved; downstream module navigation may  |
| plaintext_qti.zip | ✅ | 0.004 | 0.002 | 0 | 0 | 0 (0) | 0 (0) | 0 | 0 | 0 | 0 | no | no | 0 | 0 | 5: Missing archive reference; No supported instructional content was found in this cartrid; Resource has no usable bundled files |
| quiz_qti.zip | ✅ | 0.002 | 0.002 | 0 | 0 | 1 (1) | 0 (0) | 0 | 0 | 0 | 0 | no | no | 0 | 0 | 1: Partial quiz coverage (ibd704bbef98a99aca064aa58277e1caf) |
| rcx-1949.imscc | ✅ | 1.1 | 0.005 | 0 | 0 | 0 (0) | 0 (0) | 1 | 0 | 0 | 0 | no | no | 1 | 0 | 0 |
| unicode-filename-test-export.imscc | ✅ | 0.003 | 0.003 | 0 | 0 | 0 (0) | 0 (0) | 0 | 1 | 0 | 0 | no | yes | 3 | 0 | 1: Module metadata preserved; downstream module navigation may  |
| whatthebackslash.zip | ✅ | 0.001 | 0.001 | 0 | 0 | 0 (0) | 0 (0) | 0 | 0 | 0 | 0 | no | no | 1 | 0 | 1: No supported instructional content was found in this cartrid |

27 imported, 5 failed.
