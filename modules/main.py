import dagster as dg
import pandas as pd
from modules.utilities import read_csv, save_csv
from modules.catalog import catalog_main

read_images = read_csv.alias("images")
read_camera = read_csv.alias("camera")



@dg.pipeline
def main():
    images_df = read_images()
    camera_df = read_camera()
    #camera_geojson = read_geojson()
    catalog_df= catalog_main()
    # metadata_df = merge(images_csv, camera_csv)
    # save_csv(metadata_df)
    # omeka = omeka(metadata)
    # wiki = wiki(metadata)
    save_csv(images_df)
    save_csv(camera_df)
    save_csv(catalog_df)
