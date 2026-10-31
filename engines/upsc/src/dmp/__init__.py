"""Daily Mains Practice — UPSC Mains questions and model answers from a finished note.

The layer sits AFTER the notes pipeline and reads its output; nothing in here is
imported by the generation path, so a failure in practice generation can never
affect the notes a user already has.

The split between `generate_questions` and `generate_answers` is the whole
design. Questions cost three model calls for all ten; an answer costs one
call each. Generating the answers up front would quintuple the bill of every
run for answers nobody asked to read — so questions arrive together and answers
arrive one at a time, only for the boxes the user ticked.
"""
