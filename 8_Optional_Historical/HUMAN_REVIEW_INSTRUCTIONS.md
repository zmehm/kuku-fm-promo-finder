# Human review - next actions

The review is integrated, but no actual human choices have been supplied yet. It currently has 0/30 ratings. Do not use model-generated ratings as human evidence.

1. Give three different people reviewer_1.zip, reviewer_2.zip, and reviewer_3.zip from 4_Review_and_Walkthrough. Give each only their own ZIP. Keep private answer keys, model scores, and assisted assessments hidden.
2. Each person opens review.html, compares ten pairs, and fills ratings.csv. Keep reviewer_id and pair_id unchanged. Choices must be A, B, Tie, or Neither. Provide an overall choice and reason; hook, cliffhanger, and clarity choices are encouraged. Leaving undecided overall choices blank is allowed and remains pending.
3. Run streamlit run app.py and open Business & Baseline Analytics. Scroll to Current blind human review. Choose the assigned reviewer, upload their returned CSV, confirm its human origin, and click Validate and save returned ratings. Wrong assignments, invalid choices, and missing pair IDs are rejected before anything is overwritten. Earlier sheets are backed up privately.
4. The current result summary updates from the actual CSVs. Pending and partial results are labelled explicitly. Click Update solution PDF from current ratings to regenerate the document and download it. This cannot authenticate who authored a CSV; you remain responsible for honest provenance.
5. Rebuild the reviewer package after collecting ratings: python package_submission.py. The packaging command also refreshes the combined submission ZIP. Keep your own walkthrough and independent-input test separate from human preference ratings.

CLI alternative:
python review_tools.py import-ratings --reviewer reviewer_1 --file path/to/returned.csv
python review_tools.py score
python generate_pdf.py
python package_submission.py

This is a small, script-only comparison among episodes already recommended by the tool. It is not representative of all shows, proof of video engagement, or an ad-conversion test. Ratings on one episode are clustered; report episode-majority outcomes and losses as well as wins.
