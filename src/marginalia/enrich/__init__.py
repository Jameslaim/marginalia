"""What happens to a transcript after it is stored.

`note` writes the understanding layer; `timestamps` aligns cited timestamps back
onto the transcript line they refer to. Both are regenerable from stored
transcripts with zero network calls, and neither can reach a provider.
"""
