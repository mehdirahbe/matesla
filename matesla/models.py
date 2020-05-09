# don't forget to add your models in admin.py of you want an administration page

'''From to clean all migrations:
https://simpleisbetterthancomplex.com/tutorial/2016/07/26/how-to-reset-migrations.html

1. Remove the all migrations files within your project

Go through each of your projects apps migration folder and remove everything inside, except the __init__.py file.

Or if you are using a unix-like OS you can run the following script (inside your project dir):

find . -path "*/migrations/*.py" -not -name "__init__.py" -delete
find . -path "*/migrations/*.pyc"  -delete

2. Drop the current database, or delete the db.sqlite3 if it is your case.
3. Create the initial migrations and generate the database schema:

python manage.py makemigrations
python manage.py migrate

And you are good to go.
'''


# Create your models in models subdir
# see https://code.djangoproject.com/wiki/CookBookSplitModelsToFiles

