import pyautogui
import time
import cv2
import numpy as np

items = [
    "bo",
    "carot",
    "daunanh",
    "ga",
    "heo",
    "huongduong",
    "luami",
    "mia",
    "de",
    "bap",
]

time.sleep(1)
iml = pyautogui.screenshot(region=(510, 200, 850, 600))
iml.save("screenshot.png")

screenshoot_img = cv2.imread("screenshot.png", cv2.IMREAD_COLOR)
assert screenshoot_img is not None, "Failed to load screenshot image."

screenshot_hsv = cv2.cvtColor(screenshoot_img, cv2.COLOR_BGR2HSV)

for item in items:
    for level in [1, 2, 3]:
        template = cv2.imread(f"images/{item}{level}.png", cv2.IMREAD_COLOR)
        assert template is not None, f"Failed to load images/{item}{level}.png"

        template_hsv = cv2.cvtColor(template, cv2.COLOR_BGR2HSV)

        result = cv2.matchTemplate(screenshot_hsv, template_hsv, cv2.TM_CCOEFF_NORMED)
        threshold = 0.7
        locations = np.where(result >= threshold)

        rectangles = []
        for loc in zip(*locations[::-1]):
            rect = [
                int(loc[0]),
                int(loc[1]),
                int(template_hsv.shape[1]),
                int(template_hsv.shape[0]),
            ]
            rectangles.append(rect)
            rectangles.append(rect)
        rectangles, weights = cv2.groupRectangles(rectangles, groupThreshold=1, eps=0.5)
        for x, y, w, h in rectangles:
            cv2.rectangle(screenshoot_img, (x, y), (x + w, y + h), (0, 255, 0), 2)
            # add text inside the rectangle
            cv2.putText(
                screenshoot_img,
                f"{item}_{level}",
                (x, y - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 0),
                1,
                cv2.LINE_AA,
            )
# save img
cv2.imwrite("after.png", screenshoot_img)
cv2.imshow("Detected Items", screenshoot_img)
cv2.waitKey(0)
cv2.destroyAllWindows()

# time.sleep(1)
# # move all to first match
# if len(rectangles) > 0:
#     first_x, first_y, _, _ = rectangles[0]
#     for x, y, w, h in rectangles[1:]:
#         pyautogui.moveTo(
#             x + w // 2 + 316, y + h // 2 + 158
#         )  # Adjust for the screenshot region
#         pyautogui.dragTo(first_x + w // 2 + 316, first_y + h // 2 + 158, duration=0.2)
#         time.sleep(0.5)  # Add a small delay between moves
#         break
