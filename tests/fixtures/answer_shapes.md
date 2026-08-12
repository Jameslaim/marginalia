Synthetic citation shapes for parser regression tests, preserving Unicode dashes
(U+2011 non-breaking hyphen and U+2013 en-dash) and punctuation edge cases.

Do not "tidy" this file. The U+2011 characters inside the video IDs and the
U+2013 range separators are the point — a charset that cannot see them does not
fail loudly, it matches something else.

Synthetic Talk A sets a one-hour figure [AMwrLF_TeWo 00:00:42–00:01:02]
and two synthetic talks disagree about the discount [GfNdXFS8O0w 00:00:37; 6cEQEba0i2A 00:02:29].

A range in MM:SS [HDMqDV7mmGo 02:39–03:37] is a link to its start.

An ID that legitimately begins with a hyphen, followed by a comma list where the
second entry inherits the video [‑YTupW6YG1k 01:00–01:09, 02:28].

A Unicode hyphen in the middle of an ID [iywyv‑ZzfeA 01:02].

Stray punctuation in front of the ID [‑‑_0uiiJfsBPI 15:48–16:40].

Prose trailing inside the bracket [‑YTupW6YG1k 00:00–00:42? — under-specifies; the
figure is never revisited].

The two that the strict parser did recover [GfNdXFS8O0w 02:27] and [tWJzCxV6Aqc 04:40].

Round brackets, which models emit frequently (dTh5fnB9CE8 00:01:09).

Trailing milliseconds, as `snap-timestamps` writes them [skXT6WFNQnc 01:36.600].

Ordinary parentheses must not be mistaken for citations (see above) and neither
should a ratio like 2:1 or a price of $4 per million.

A video that is not in the library must never become a link [zzzzzzzzzzz 01:00].
