# Balanced quality scorecard

Authored development suite, not independent held-out data. Labels are authored expectations, not collected human ratings. Precision/recall measure the narrow heuristic, not general creative quality. Misses and false alarms are retained.

## closure

{"true_positive": 4, "false_positive": 1, "false_negative": 2, "true_negative": 5, "precision": 0.8, "recall": 0.667, "false_positive_rate": 0.167, "n": 12}

- Miss / false alarm: implicit_ring (expected=True, detected=False)
- Miss / false alarm: implicit_delivery (expected=True, detected=False)
- Miss / false alarm: threatening_over (expected=False, detected=True)
## delayed_hook

{"true_positive": 3, "false_positive": 0, "false_negative": 0, "true_negative": 3, "precision": 1.0, "recall": 1.0, "false_positive_rate": 0.0, "n": 6}

## offline_abstention

{"true_positive": 2, "false_positive": 0, "false_negative": 1, "true_negative": 3, "precision": 1.0, "recall": 0.667, "false_positive_rate": 0.0, "n": 6}

- Miss / false alarm: quiet_misleading_emotions (expected=True, detected=False)
