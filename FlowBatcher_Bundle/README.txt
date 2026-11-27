=========================================
      FLOW BATCHER - QUICK GUIDE
=========================================

--- ONE-TIME SETUP (Takes 1 Minute) ---

1. Extract this ZIP folder to your Desktop or Documents (do NOT run it from inside the ZIP).
2. Open Google Chrome and type this in the address bar:
   chrome://extensions
3. Turn ON "Developer mode" (switch in the top-right corner).
4. Click "Load unpacked" (top-left) and select the "flow-extension" folder inside this bundle.
5. Done! You never have to do this setup again.


--- HOW TO USE (Every Time) ---

1. Open Google Chrome and go to:
   https://flow.google.com
   Make sure you are signed in to your Google account and open any project.
   (Keep this Chrome tab open while generating.)

2. Double-click "FlowBatcher.exe" inside this folder.
   * Note: The first time you open it, Windows may show a blue popup saying
     "Windows protected your PC". Click "More info" -> "Run anyway".

3. A web page called "Flow Batcher" will open automatically in your browser.

4. Paste your prompts into the box (ONE prompt per line).

5. Choose your Model:
   - Try "Lite (harbor_seal)" or "Auto (Account Default)" first.
   - If you have a Pro account, you can also use "Pro (gem_pix_2)".
   - Choose your Aspect Ratio (16:9, 9:16, 1:1, etc.).

6. Click "Start Generating".
   - Each batch automatically saves into a brand-new folder inside "numbered_output"
     (example: numbered_output/batch_2026-11-21_21-30-00/).
   - Images are strictly numbered 001.jpg, 002.jpg, 003.jpg matching your prompt order.
   - Click "Open Output Folder" to view your images.

7. If you need to stop mid-batch, click the red "Stop" button.
   If any image shows a red "ERROR" or "CANCELLED" badge:
   - Wait for the batch to finish (or stop it), optionally switch Model/Aspect, then click
     the orange "Retry Failed" / "Retry Unfinished" button.
   - It will only re-run unfinished prompts and save them with their exact original numbers.

8. When you are completely finished:
   - Click the red "Quit App" button at the top-right before closing the tab.


--- TROUBLESHOOTING ---

- Everything fails immediately?
  Make sure your Google Flow tab is open in Chrome and try switching the Model dropdown
  between "Lite", "Standard", and "Auto".

- Browser tab closed accidentally?
  Just open http://127.0.0.1:8765 in Chrome to get back to the page.