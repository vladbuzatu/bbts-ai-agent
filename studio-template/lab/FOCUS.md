# CarStage lab: focus and rules (set by the owner)

Every lab user (the model-lab agent and the PM) reads this through `carstage_lab_info`.
To change the focus, edit this file (on the server: inside the container at /studio/lab/FOCUS.md).

## The three flows in focus
1. **optimal**: local YOLO cutout + matting + wheel shadows, one Gemini composite at the end.
2. **gemini_compose**: one-shot Gemini edit (car + stage + prompt).
3. **openai_compose with gpt-image-2**: STANDARD with the new model (the lab applies
   OPENAI_IMAGE_EDIT_MODEL=gpt-image-2 by default).
SEC (ADVANCED), photoreal, computational and rges are out of scope; the lab refuses them.

## Hard rule: the number plate stays original
- The plate must be pixel-for-pixel the original plate: characters, EU strip, colours, plate frame and dealer
  text on it. Not redrawn, not blurred, not "cleaned", not blanked.
- Check EVERY result: find the plate in the original, align the result to the original car (OpenCV feature
  matching + homography on the car region), crop the plate area from both at full resolution, compare
  (SSIM and the absolute difference), and look at both crops side by side. Any visible change = FAIL.
- A failed image disqualifies that variant for that photo. A variant that fails the plate check on more
  than 1 photo in 10 can't be recommended, whatever else it does well.
- A flow may still pass if a plate-restore step puts the ORIGINAL plate pixels back afterwards
  (see "Image-processing variations"). Report such results as "<flow> + plate restore" and say that the
  restore step has to be added to CarStage's code for that flow.

## Explore within the budget
The budget is per testing round (the owner's next message starts a new round). Plan each round to use
at most ~90% of it, say what the round tests and its estimated cost, and stop early on clear losers:
run new variants on 2-3 hard photos first; only variants that pass the plate rule and look competitive
get the full photo set (successive halving).

Model variations worth testing (check ids with carstage_lab_info / provider docs first):
- gemini_compose: GEMINI_IMAGE_MODEL = gemini-3.1-flash-image-preview (default), gemini-3.1-flash-image,
  gemini-3-pro-image, newer Nano Banana ids; prompt variants that explicitly forbid touching the plate,
  badges and car body, and that ask for a non-cropped, centred car.
- openai_compose: OPENAI_IMAGE_EDIT_QUALITY low / medium / high, OPENAI_IMAGE_EDIT_SIZE non-square
  (e.g. 1536x1024) so the stage isn't cropped, OPENAI_USE_REMBG_MASK on/off, prompt variants.
- optimal: GEMINI_SEC_BACKGROUND_FIT_MODEL (pro vs flash image), OPTIMAL_TRIMAP_ERODE_PX /
  OPTIMAL_TRIMAP_DILATE_PX, other OPTIMAL_* settings listed by carstage_lab_info. Make sure the YOLO model
  is used (meta.json mask source); a Gemini-fallback mask means the model file is missing: tell the owner.

## Image-processing variations (free: no API calls)
You may post-process results locally with /opt/lab-venv/bin/python (OpenCV, numpy, scikit-image, rembg,
pymatting) and import CarStage's local modules (for example compose_api/plate_restore.py, the cutout and
mask-refine code) as long as they make no API calls. Useful: restore the original plate (and badges) by
warping the original pixels into the result, refine edges/halos with matting, re-add a contact shadow.
Keep the scripts in research/scripts/ so the PM can port the winning steps into CarStage.
