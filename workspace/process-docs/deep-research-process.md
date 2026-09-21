<!-- Constructed example for the demo; not a customer document. -->

# How we do deep research reports

Last updated by the delivery team. This is the process we follow when a client asks us for a research report on a topic. It has grown out of habit more than design, so some of it is written down here for the first time.

## When a request comes in

A client sends us a topic, usually one line in an email or a Slack message. Something like "durable execution platforms for AI agents, who leads and why". Whoever picks it up creates a folder for it and pastes the topic into the brief template.

We don't usually go back to the client to clarify the topic at this point. If the topic is very short we make our best guess at what they want and note our assumptions in the brief.

## Writing the brief

The person running the report turns the topic into a set of questions the report should answer. Three to five questions is about right. The brief also says what kind of sources we're looking for and what we're leaving out. Someone senior glances at the brief before any searching starts, because if the brief is wrong everything after it is wasted effort.

## Doing the research

The researcher searches the web for each question. We prefer primary sources: the vendor's own documentation, engineering blog posts from people who have actually run the thing, funding announcements, published benchmarks. News coverage and analyst pages are fine when there's nothing better, but they should be marked as secondary.

Vendor rankings and "top 10" lists are not evidence. Every one of them ranks its own product first. We note that we saw them and move on.

The researcher keeps going until each question has a couple of solid sources or until they've spent about an hour. They keep a rough log of what they searched and what they found so the writer knows where the evidence is thin.

## Writing the report

The writer takes the brief and the research notes and writes the report. One section per question, in the order of the brief, plus a short introduction and a closing section on what's still uncertain.

Every factual claim gets a citation. At the end of the report there is a sources table with one row per citation: the line the claim is on, a few words describing the claim, the URL, and a link status column saying whether the link opens. The writer fills in the whole table, including the link status column, so the reviewer can see at a glance which sources are reachable.

## Checking the links

After the report is written, someone runs through the sources table and makes sure the links work. Anything that doesn't open gets flagged.

## Review

A reviewer who wasn't involved in the research reads the report against the brief. They check that each section actually answers its question, that the claims are backed by the sources cited, and that nothing important is missing.

The reviewer decides whether more research is needed. If they think so, they write down what's missing and the researcher goes back and does another pass on those points. If the report just needs edits, the reviewer lists them and the writer makes them. The reviewer can also reject the report if it's not salvageable, which has happened a couple of times.

When the reviewer is happy, the report is approved.

## Delivery

The approved report is exported to PDF with our template. Follow-up research, if there was any, is appended as a section at the end. Then we send the final report to the client.

## Notes

- We've had a case where all the links in a report worked when we checked but several had died by the time the client read it a week later. We don't have a fix for this yet.
- Reports typically take two to four days end to end. The research is the slow part.
- If a client comes back with questions the report didn't cover, we treat that as a new request.
