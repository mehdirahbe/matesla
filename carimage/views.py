from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse, HttpResponseRedirect
from django.core.files import File
from urllib.request import urlopen
from tempfile import NamedTemporaryFile

from carimage.models import TeslaImage

'''
Tesla uses an image generator which take a few args and return car image
Here is site which allows to play with the args:
https://observablehq.com/@slickplaid/model-3-configurator

The tesla URL is https://static-assets.tesla.com/configurator/compositor

Exemple of valid URL
Model 3
https://static-assets.tesla.com/configurator/compositor?&options=$PMNG,$W39B,$DV4W,$MT304,$IN3PB&view=STUD_3QTR&model=m3&size=200&bkba_opt=1&version=0.0.25
Model S
https://static-assets.tesla.com/configurator/compositor?&options=$WTAS,$PPMR,$MTS03&view=STUD_3QTR_V2&model=ms&size=300&bkba_opt=1&version=v0027d202004163351
Model X
https://static-assets.tesla.com/configurator/compositor?&options=$WT20,$PPMR,$MTX03&view=STUD_3QTR_V2&model=mx&size=300&bkba_opt=1&version=v0027d202004163351

Here are relevant options:

    Query Parameters:
        options - choose what options & accessories to display
        view - choose angle of view of the car
        model - which car to show
        size - photo size
        bkba_opt - background
        version - unknown (value is always 0.0.25)

BKBA_OPT Values

    0 - white background
    1 - transparent background

VIEW Values

    STUD_3QTR - 3-quarter front view
    STUD_SEAT - interior
    STUD_SIDE - side view
    STUD_REAR - shows trunk & spoiler
    STUD_WHEEL - side view of front half of car

OPTION Values

All of these options are one value, comma-separated.
Color (choose one)

    $PBSB - Solid Black
    $PPMR - Red Multi-Coat
    $PMNG - Midnight Silver Metallic (Mid-Night Gray :D)
    $PPSB - Deep Blue Metallic
    $PPSW - Pearl White Multi-Coat
    $PMSS - Silver Metallic
    $PMBL - Obsidian Black

Wheel (choose one)

    $W38B - 18" Aero Wheels
    $W39B - 19" Sport Wheels
    $W32B - 20" Sport Wheels (doesn't work for STUD_SIDE)

Spoiler

    $SLR1 - Carbon-fiber spoiler

AWD/RWD (choose one)

    $DV2W - RWD
    $DV4W - AWD

Suspension (choose one)

    $MT302 - Shows with RWD, highest
    $MT303 - Shows with AWD, low
    $MT304 - Shows with Performance, lowest

Interior (choose one)

    $PFP31 - White interior
    $IN3PB - Black interior (for STUD_SEAT)
    $IN3PW - White interior (for STUD_SEAT)

'''

'''
Here is the info returned by my car.
'AD15,MDL3,PBSB,RENA,BT37,ID3W,RF3G,S3PB,DRLH,DV2W,W39B,APF0,COUS,BC3B,CH07,PC30,FC3P,FG31,GLFR,
HL31,HM31,IL31,LTPB,MR31,FM3B,RS3H,SA3P,STCP,SC04,SU3C,T3CA,TW00,TM00,UT3P,WR00,AU3P,APH3,AF00,ZCST,MI00,CDM0'

See for explanations, bit he signals that codes are not reliable anymore
https://tesla-api.timdorr.com/vehicle/optioncodes
'''


def CreateImageFile(image):
    img_temp = NamedTemporaryFile(delete=True)
    # get from tesla and save
    img_temp.write(urlopen(image.image_url).read())
    img_temp.flush()
    image.image_file.save(f"image_{image.pk}", File(img_temp))
    # save in the db
    image.save()


# for url parse, see https://docs.djangoproject.com/en/3.0/topics/http/urls/
# color comes from codes as it seems correct and is the string we want (at least on my car)
# wheel and carmodel comes from car info, not from car info codes due to reliability
def CarImageFromTesla(request, color, wheel, CarModel):
    size = "400"

    # Assume 19 as I can only identify 18 reliably as it is my car wheels
    wheelToUse = "W39B"
    if wheel == "Pinwheel18":
        wheelToUse = "W38B"

    # Assume model 3 as I have no idea of the params for other cars
    if CarModel == "modelx":
        CarModelToUse = "mx"
        url = "https://static-assets.tesla.com/configurator/compositor?&options=$WT20,$" + color + ",$MTX03&view=STUD_3QTR_V2&model=" + CarModelToUse + "&size=" + size + "&bkba_opt=1&version=v0027d202004163351"
    else:
        if CarModel == "models" or CarModel == "models2":
            CarModelToUse = "ms"
            url = "https://static-assets.tesla.com/configurator/compositor?&options=$WTAS,$" + color + ",$MTS03&view=STUD_3QTR_V2&model=" + CarModelToUse + "&size=" + size + "&bkba_opt=1&version=v0027d202004163351"
        else:
            if CarModel == "modely":
                CarModelToUse = "my"
                url = "https://static-assets.tesla.com/configurator/compositor?&options=$WY19B,$" + color + ",$DV4W,$MTY03,$INYPB&view=STUD_3QTR&model=" + CarModelToUse + "&size=" + size + "&bkba_opt=1&version=v0027d202004163351"
            else:  # model 3
                CarModelToUse = "m3"
                url = "https://static-assets.tesla.com/configurator/compositor?&options=$" + color + ",$" + wheelToUse + ",$DV4W,$MT303,$IN3PB&view=STUD_3QTR&model=" + CarModelToUse + "&size=" + size + "&bkba_opt=1&version=0.0.25"

    # Get the image from cache, if there is a problem, redirect to tesla site
    # code inpired from https://stackoverflow.com/questions/16381241/django-save-image-from-url-and-connect-with-imagefield
    willNeedFileCreation = False
    try:
        try:
            # get from cache
            image = TeslaImage.objects.get(image_url=url)
        except ObjectDoesNotExist:
            # add it with image from tesla
            image = TeslaImage()
            image.image_url = url
            # save in the db in order that primary key is init
            image.save()
            willNeedFileCreation = True
        # check if a file with image is present. Will be absent on initial
        # and also when image dir is cleaned
        if image.image_url and not image.image_file:
            willNeedFileCreation = True
        if willNeedFileCreation:
            CreateImageFile(image)
        # Try to return cache entry
        try:
            return HttpResponse(image.image_file, content_type="image/png")
        except Exception:
            CreateImageFile(image)  # image was surely missing, seems that test on image.image_file fails
            return HttpResponse(image.image_file, content_type="image/png")
    except Exception:
        # go to tesla site
        return HttpResponseRedirect(url)

# valid url example:
# http://127.0.0.1:8000/carimage/PBSB/Pinwheel18/model3
