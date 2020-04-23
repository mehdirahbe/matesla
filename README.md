# matesla
Python/django website using tesla api to connect to your marvellous Tesla car

Useful files:
1) Procedures calling tesla api, if you need something not yet available, it there that you have to adapt code
https://github.com/mehdirahbe/matesla/blob/master/matesla/TeslaConnect.py

2) Views which return the HTML content displayed in browser.
https://github.com/mehdirahbe/matesla/blob/master/matesla/views.py

3) List all URL available. Contain the URL, the view to use and a short name used to reference it from HTML
https://github.com/mehdirahbe/matesla/blob/master/matesla/urls.py

4) The HTML with car status page
https://github.com/mehdirahbe/matesla/blob/master/matesla/templates/matesla/carstatus.html

5) The base of all HTML rendering, it contains the formatting in the form of CSS
https://github.com/mehdirahbe/matesla/blob/master/templates/base.html

How to:
1) To add a new link, you have to adapt urls.py, views.py (to serve it) and carstatus.html (to display the link). See for example sentry or door lock.

2) If you want to display an additional information on the car, you probably only have to adapt carstatus.html, except if it is a computed value (such as battery degradattion) where you will also have to adapt views.py to compute the value and put it in the context passed to rendering.

3) Change look: adapt CSS in base.html

Todo:
1) Add french translation
Done
2) Stop storing (even if encrypted) tesla user/PW
Done
3) Add stats on firmware updates and autopilot HW updates

