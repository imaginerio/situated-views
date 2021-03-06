import math
import os
import re
import shutil
from solids.utils import df_csv_io_manager
import sys
from typing import List
from dotenv import load_dotenv

import dagster as dg
import geojson
import mercantile
import numpy as np
import pandas as pd
import requests
from dagster.config.config_type import String
from PIL import Image
from pykml import parser
from pyproj import Proj
from shapely.geometry import Point, Polygon
from SPARQLWrapper import JSON, SPARQLWrapper

load_dotenv(override=True)


def find_with_re(property, kml):
    return re.search(f"(?<=<{property}>).+(?=<\/{property}>)", kml).group(0)


def reproject(coordinates, inverse=False):
    rj = Proj("EPSG:32722")
    origin = Point(coordinates)
    origin_proj = rj(origin.x, origin.y, inverse=inverse)

    return Point(origin_proj)


def query_wikidata(Q):
    endpoint_url = "https://query.wikidata.org/sparql"

    query = """SELECT ?coordinate
        WHERE
        {
        wd:%s wdt:P625 ?coordinate .
        }""" % (
        Q
    )

    def get_results(endpoint_url, query):
        user_agent = "WDQS-example Python/%s.%s" % (
            sys.version_info[0],
            sys.version_info[1],
        )
        # TODO adjust user agent; see https://w.wiki/CX6
        sparql = SPARQLWrapper(endpoint_url, agent=user_agent)
        sparql.setQuery(query)
        sparql.setReturnFormat(JSON)
        return sparql.query().convert()

    results = get_results(endpoint_url, query)
    result_list = []
    for result in results["results"]["bindings"]:
        if result:
            result_list.append(result["coordinate"]["value"])
    return result_list


def get_radius(kml):
    with open(kml, "r") as f:

        KML = parser.parse(f).getroot()
    id = str(KML.PhotoOverlay.name)
    tilt = KML.PhotoOverlay.Camera.tilt
    df = pd.read_csv(
        os.environ["METADATA"],
        index_col="id",
    )
    depicts = df.loc[id, "wikidata_depict"]
    if isinstance(depicts, str):
        depicts = depicts.split("||")
        distances = []
        points = []
        for depict in depicts:
            q = re.search("(?<=\/)Q\d+", depict).group(0)
            point = query_wikidata(q)
            if point:
                points.append(point[0])
            else:
                continue
            for point in points:

                lnglat = re.search("\((-\d+\.\d+) (-\d+\.\d+)\)", point)
                lng = lnglat.group(1)
                lat = lnglat.group(2)
                depicted = reproject((float(lng), float(lat)))
                origin = reproject(
                    (
                        KML.PhotoOverlay.Camera.longitude,
                        KML.PhotoOverlay.Camera.latitude,
                    )
                )

                distance = origin.distance(depicted)
                distances.append(distance)

        if distances:
            radius = max(distances)

        else:

            return None
    else:
        if tilt <= 89:
            tan = math.tan((tilt * math.pi) / 180)
            radius = KML.PhotoOverlay.Camera.altitude * tan
            if radius < 400:

                return None
        else:

            return None

    return radius


def draw_cone(kml, radius=400, steps=200):

    with open(kml, "r") as f:
        KML = parser.parse(f).getroot()

    camera = KML.PhotoOverlay.Camera
    viewvolume = KML.PhotoOverlay.ViewVolume
    center = Point(reproject((camera.longitude, camera.latitude)))
    start_angle = camera.heading - viewvolume.rightFov
    end_angle = camera.heading - viewvolume.leftFov

    def polar_point(origin_point, angle, distance):
        return [
            origin_point.x + math.sin(math.radians(angle)) * distance,
            origin_point.y + math.cos(math.radians(angle)) * distance,
        ]

    if start_angle > end_angle:
        start_angle = start_angle - 360
    else:
        pass
    step_angle_width = (end_angle - start_angle) / steps
    sector_width = end_angle - start_angle
    segment_vertices = []
    segment_vertices.append(reproject(polar_point(center, 0, 0), inverse=True))
    segment_vertices.append(
        reproject(polar_point(center, start_angle, radius), inverse=True)
    )
    for z in range(1, steps):
        segment_vertices.append(
            (
                reproject(
                    polar_point(center, start_angle + z * step_angle_width, radius),
                    inverse=True,
                )
            )
        )
    segment_vertices.append(
        reproject(polar_point(center, start_angle + sector_width, radius), inverse=True)
    )
    segment_vertices.append(reproject(polar_point(center, 0, 0), inverse=True))

    return Polygon(segment_vertices)


@dg.solid(config_schema=dg.StringSource)
def get_list(context):
    path = context.solid_config
    list_kmls = os.listdir(path)
    kmls = []
    for kml in list_kmls:
        full_path = os.path.join(path, kml)
        kmls.append(full_path)
    list_kmls = [x for x in kmls if x != "data/input/kmls/new_raw/.gitkeep"]

    return list_kmls


@dg.solid(config_schema=dg.StringSource)
def split_photooverlays(context, kmls, delete_original=False):
    path = context.solid_config
    splited_kmls = []
    photooverlays = ""

    for kml in kmls:
        splited_kmls.append(kml)
        with open(kml, "r") as f:
            txt = f.read()
            if re.search("<Folder>", txt):
                header = "\n".join(txt.split("\n")[:2])
                photooverlays = re.split(".(?=<PhotoOverlay>)", txt)[1:]
                photooverlays[-1] = re.sub("</Folder>\n</kml>", "", photooverlays[-1])

        for po in photooverlays:
            filename = find_with_re("name", po)
            with open(os.path.join(path, filename + ".kml"), "w") as k:
                k.write(f"{header}\n{po}</kml>")
        if delete_original:
            os.remove(os.path.abspath(kml))
        shutil.move(kml, "data/input/kmls/processed_raw")


@dg.solid(config_schema=dg.StringSource)
def change_img_href(context):
    path = context.solid_config
    kmls = [
        os.path.join(path, file)
        for file in os.listdir(path)
        if os.path.isfile(os.path.join(path, file))
    ]
    list_kmls = [x for x in kmls if x != "data/input/kmls/new_single/.gitkeep"]

    for kml in list_kmls:
        with open(kml, "r+") as f:
            txt = f.read()
            filename = find_with_re("name", txt)
            txt = re.sub(
                "(?<=<href>).+(?=<\/href>\n\t+<\/Icon>\n\t+<ViewVolume>)",
                f"https://images.imaginerio.org/iiif-img/{filename}/full/^1200,/0/default.jpg",
                txt,
            )
            f.seek(0)
            f.write(txt)
            f.truncate()

    return list_kmls


@dg.solid
def correct_altitude_mode(context, kmls):

    for kml in kmls:
        with open(kml, "r+") as f:
            txt = f.read()
            if re.search("(?<=altitudeMode>)relative(.+)?(?=\/altitudeMode>)", txt):
                lat = round(float(find_with_re("latitude", txt)), 5)
                lng = round(float(find_with_re("longitude", txt)), 5)
                alt = round(float(find_with_re("altitude", txt)), 5)
                z = 15
                tile = mercantile.tile(lng, lat, z)
                westmost, southmost, eastmost, northmost = mercantile.bounds(tile)
                pixel_column = np.interp(lng, [westmost, eastmost], [0, 256])
                pixel_row = np.interp(lat, [southmost, northmost], [256, 0])
                tile_img = Image.open(
                    requests.get(
                        "https://api.mapbox.com/v4/mapbox.terrain-rgb/10/800/200.pngraw?access_token=pk.eyJ1IjoibWFydGltcGFzc29zIiwiYSI6ImNra3pmN2QxajBiYWUycW55N3E1dG1tcTEifQ.JFKSI85oP7M2gbeUTaUfQQ",
                        stream=True,
                    ).raw
                ).load()

                R, G, B, _ = tile_img[int(pixel_row), int(pixel_column)]
                height = -10000 + ((R * 256 * 256 + G * 256 + B) * 0.1)
                new_height = height + alt
                txt = re.sub(
                    "(?<=<altitudeMode>).+(?=<\/altitudeMode>)", "absolute", txt
                )
                txt = re.sub("(?<=<altitude>).+(?=<\/altitude>)", f"{new_height}", txt)
                txt = re.sub(
                    "(?<=<coordinates>).+(?=<\/coordinates>)",
                    f"{lng},{lat},{new_height}",
                    txt,
                )

                f.seek(0)
                f.write(txt)
                f.truncate()
            else:
                continue
    return kmls


@dg.solid(
    input_defs=[dg.InputDefinition("metadata", root_manager_key="metadata_root")],
)
def create_feature(context, kmls, metadata):
    new_features = []
    processed_ids = []
    metadata["upper_ids"] = metadata["id"].str.upper()
    metadata = metadata.set_index("upper_ids")
    # Id = ""

    for kml in kmls:
        try:
            with open(kml, "r") as f:
                KML = parser.parse(f).getroot()
                Id = (str(KML.PhotoOverlay.name)).upper()
                created = metadata.loc[Id, "date_created"]
                circa = (
                    ""
                    if pd.isna(metadata.loc[Id, "date_circa"])
                    else str(metadata.loc[Id, "date_circa"])
                )
                accurate = pd.notna(metadata.loc[Id, "date_created"])
                properties = {
                    "id": metadata.loc[Id, "id"],
                    "title": ""
                    if pd.isna(metadata.loc[Id, "title"])
                    else str(metadata.loc[Id, "title"]),
                    "description": ""
                    if pd.isna(metadata.loc[Id, "description"])
                    else str(metadata.loc[Id, "description"]),
                    "creator": ""
                    if pd.isna(metadata.loc[Id, "creator"])
                    else str(metadata.loc[Id, "creator"]),
                    "first_year": ""
                    if pd.isna(metadata.loc[Id, "first_year"])
                    else str(int(metadata.loc[Id, "first_year"])),
                    "last_year": ""
                    if pd.isna(metadata.loc[Id, "last_year"])
                    else str(int(metadata.loc[Id, "last_year"])),
                    "source": "Instituto Moreira Salles",
                    "longitude": str(round(float(KML.PhotoOverlay.Camera.longitude),5)),
                    "latitude": str(round(float(KML.PhotoOverlay.Camera.latitude),5)),
                    "altitude": str(round(float(KML.PhotoOverlay.Camera.altitude),5)),
                    "heading": str(round(float(KML.PhotoOverlay.Camera.heading),5)),
                    "tilt": str(round(float(KML.PhotoOverlay.Camera.tilt),5)),
                    "fov": str(
                        abs(float(KML.PhotoOverlay.ViewVolume.leftFov))
                        + abs(float(KML.PhotoOverlay.ViewVolume.rightFov))
                    ),
                }

                if accurate:
                    properties["date_created"] = created
                else:
                    properties["date_circa"] = circa

                radius = get_radius(kml)
                print(f"OK: {Id}")
                if radius:
                    viewcone = draw_cone(kml, radius=radius)
                else:
                    viewcone = draw_cone(kml)
                new_features.append(
                    geojson.Feature(geometry=viewcone, properties=properties)
                )
                processed_ids.append(Id)

        except Exception as E:
            print(f"ERROR: {E} no ID: {Id}")

    return new_features


@dg.solid(config_schema=dg.StringSource)
def move_files(context, new_features):
    list_kmls = [feature["properties"]["id"] for feature in new_features]
    path_from = "data/input/kmls/new_single"
    path_to = context.solid_config

    for kml in list_kmls:
        try:
            kml_from = os.path.join(path_from, kml + ".kml")
            kml_to = os.path.join(path_to, kml + ".kml")
            if os.path.exists(kml_to):
                os.remove(os.path.abspath(kml_to))
                shutil.move(kml_from, path_to)
            else:
                shutil.move(kml_from, path_to)

        except Exception as e:
            print(e)

    return list


@dg.solid(
    config_schema=dg.StringSource,
    output_defs=[
        dg.OutputDefinition(io_manager_key="geojson", name="import_viewcones")
    ],
)
def create_geojson(context, new_features):
    camera = context.solid_config

    if new_features:
        if os.path.isfile(camera):
            current_features = (geojson.load(open(camera))).features
            current_ids = [feature["properties"]["id"] for feature in current_features]

            for new_feature in new_features:
                id_new = new_feature["properties"]["id"]

                if id_new in current_ids:
                    print("Updated:  ", id_new)
                    index = current_ids.index(id_new)
                    current_features[index] = new_feature

                else:
                    print("Appended: ", id_new)
                    current_features.append(new_feature)

            feature_collection = geojson.FeatureCollection(features=current_features)
            return feature_collection

        else:
            feature_collection = geojson.FeatureCollection(features=new_features)
            return feature_collection
    else:
        print("Nothing's to updated on import_viewcones")
        pass
