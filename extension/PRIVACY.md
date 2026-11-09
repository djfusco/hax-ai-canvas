# Course Consultant extension: privacy

**What it does:** when you click its button while viewing one of your Canvas
courses, it reads that course's content and, after you review it, sends it
to Course Consultant so the consultant can help you improve the course.

## What it reads
Only when you click "Read this course", and only from the Canvas tab you are
viewing, using your existing Canvas sign-in:

- Course title, code, term and syllabus
- Modules and their order, requirements and prerequisites
- Pages
- Assignments, with points, due dates, settings and rubrics
- Assignment groups and their weights, and the late policy
- Quizzes and their questions (not answers or answer keys)
- Discussion prompts written by the instructor
- Learning outcomes
- The list of course files: names, types and sizes only, never their contents

## What it never reads
Grades, submissions, student discussion replies, class lists, student
profiles, messages, analytics, or anything from other courses or other sites.
A fixed allowlist in `collect.js` enforces this: any other request is
refused, even if Canvas points to it.

## What it sends, and where
Nothing is sent until you review the summary and click **Send**. The course
then goes only to the Course Consultant site configured in this extension,
through a page you are signed in to. Before saving, Course Consultant removes
likely student information (emails, phone numbers, IDs and names in class
posts). After the import, the extension discards its copy.

## Permissions
- **activeTab:** access to the current tab only, only after you click the
  extension's button. It cannot see your other tabs or browsing.
- **scripting:** to run the reader in that tab after your click.

The extension does not store your Canvas or Course Consultant credentials,
has no background access to any website, and contains no analytics.
