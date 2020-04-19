from django.http import HttpResponse
from django.http import HttpResponseRedirect

'''
Tesla uses an image generator which take a few args and return car image
Here is site which allows to play with the args:
https://observablehq.com/@slickplaid/model-3-configurator

The tesla URL is https://static-assets.tesla.com/configurator/compositor

Exemple of valid URL
https://static-assets.tesla.com/configurator/compositor?&options=$PMNG,$W39B,$DV4W,$MT304,$IN3PB&view=STUD_3QTR&model=m3&size=200&bkba_opt=1&version=0.0.25

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

#for url parse, see https://docs.djangoproject.com/en/3.0/topics/http/urls/
# color comes from codes as it seems correct and is the string we want (at least on my car)
# wheel and carmodel comes from car info, not from car info codes due to reliability
def CarImageFromTesla(request,color,wheel,CarModel):
    size="300"

    #Assume 19 as I can only identify 18 reliably as it is my car wheels
    wheelToUse="W39B"
    if wheel=="Pinwheel18":
        wheelToUse="W38B"

    #Assume model 3 as I have no idea of the params for other cars
    CarModelToUse="m3"
    url="https://static-assets.tesla.com/configurator/compositor?&options=$"+color+",$"+wheelToUse+",$DV4W,$MT303,$IN3PB&view=STUD_3QTR&model="+CarModelToUse+"&size="+size+"&bkba_opt=1&version=0.0.25"
    return HttpResponseRedirect(url)