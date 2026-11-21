"""AI-2 mastery integration (M8): outbox delivery to studygraph.mastery/1 and persistence of validated predictions.

Core-api owns the attempts (immutable events), the delivery state and the stored
predictions; the AI service only computes. See docs/ai-mastery-integration.md.
"""
