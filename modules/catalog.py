import os
from pprint import pprint
from xml.etree import ElementTree

import numpy as np
import pandas as pd
import dagster as dg

#solids catalog
@dg.solid
def read_xml(context):    
    path = context.solid_config
    with open(path, encoding="utf8") as f:
        tree = ElementTree.parse(f)
    root = tree.getroot()

    return root   


@dg.solid  # Find the uids
def find_uids(context, root):   
    uids = {}
    for thing in root[0][0]:
        uids[thing.attrib["uid"]] = thing[0].text   

    table = {}
    for field in uids.values():
        table[field] = []

    
    outDict = {"table": table, "uids":uids}

    return outDict
    
    

@dg.solid # Fill the records
def fill_records(context,root,outDict): 
    ns = {"cumulus": "http://www.canto.com/ns/Export/1.0"} 
    for thing in root[1]:
        added = set()
        for field_value in thing.findall("cumulus:FieldValue", ns):
            try:
                if len(field_value) == 0:
                    value = field_value.text.strip()
                else:
                    value = field_value[0].text.strip().split(":")
                    value = str(value).strip("[']")

                outDict['table'][outDict['uids'][field_value.attrib["uid"]]].append(value)
                added.add(field_value.attrib["uid"])
            except KeyError:
                continue
        for missing in outDict['uids'].keys() - added:
            try:
                outDict['table'][outDict['uids'][missing]].append(None)
            except KeyError:
                continue
    formated_table = outDict['table']
    catalog_df = pd.DataFrame(formated_table)

    return catalog_df

@dg.solid # load
def load(context,df):   
    catalog_df = df.astype(
        {"DATA": str, "DATA LIMITE INFERIOR": str, "DATA LIMITE SUPERIOR": str}
    )
    catalog_df[["DATA LIMITE SUPERIOR", "DATA LIMITE INFERIOR"]] = catalog_df[
        ["DATA LIMITE SUPERIOR", "DATA LIMITE INFERIOR"]
    ].applymap(lambda x: x.split(".")[0])

    return catalog_df

@dg.solid # rename columns
def rename_columns(context,df):    
    catalog_df = df.rename(
        columns={
            "Record Name": "id",
            "TÍTULO": "title",
            "RESUMO": "description",
            "AUTORIA": "creator",
            "DATA": "date",
            "DATA LIMITE INFERIOR": "start_date",
            "DATA LIMITE SUPERIOR": "end_date",
            "DIMENSÃO": "dimensions",
            "PROCESSO FORMADOR DA IMAGEM": "fabrication_method",
            "LOCAL": "place",
            "DESIGNAÇÃO GENÉRICA": "type",
        },
    )

    return catalog_df


@dg.solid # select columns from renamed coluns of catalog df
def select_columns(context,df):    
    catalog_df = df[
        [
            "id",
            "title",
            "description",
            "creator",
            "date",
            "start_date",
            "end_date",
            "type",
            "fabrication_method",
            "dimensions",
            "place",
        ]
    ]

    return catalog_df


@dg.solid # remove file extension    
def remove_extension(context,df):
    df["id"] = df["id"].str.split(".", n=1, expand=True)
    catalog_df = df

    return catalog_df

@dg.solid # remove duplicates    
def remove_duplicates(context,df): 
    catalog_df = df.drop_duplicates(subset="id", keep="last")

    return catalog_df


@dg.solid# check dates accuracy
def dates_accuracy(context,df):
    circa = df["date"].str.contains(r"[a-z]", na=False,)
    year = df["date"].str.count(r"[\/-]") == 0
    month = df["date"].str.count(r"[\/-]") == 1
    day = df["date"].str.count(r"[\/-]") == 2
    startna = df["start_date"].isna()
    endna = df["end_date"].isna()

    df.loc[year, "date_accuracy"] = "year"
    df.loc[month, "date_accuracy"] = "month"
    df.loc[day, "date_accuracy"] = "day"
    df.loc[circa, "date_accuracy"] = "circa"

    #format date
    df["date"] = df["date"].str.extract(r"([\d\/-]*\d{4}[-\/\d]*)")
    df["start_date"] = df["start_date"].str.extract(
        r"([\d\/-]*\d{4}[-\/\d]*)"
    )
    df["end_date"] = df["end_date"].str.extract(
        r"([\d\/-]*\d{4}[-\/\d]*)"
    )
    df[["date", "start_date", "end_date"]] = df[
        ["date", "start_date", "end_date"]
    ].applymap(lambda x: pd.to_datetime(x, errors="coerce", yearfirst=True))

    #fill dates
    df.loc[circa & startna, "start_date"] = df["date"] - pd.DateOffset(
        years=5
    )
    df.loc[circa & endna, "end_date"] = df["date"] + pd.DateOffset(
        years=5
    )
    df.loc[startna, "start_date"] = df["date"]
    df.loc[endna, "end_date"] = df["date"]

    catalog_df = df

    return catalog_df

@dg.solid   # reverse cretor name
def reverse_creators_name(context,df):
    df["creator"] = df["creator"].str.replace(r"(.+),\s+(.+)", r"\2 \1")
    catalog_df = df

    return catalog_df
 
@dg.solid   # save list of creators for rights assessment
def creators_list(context,df):
    listed_creators = df["creator"].unique()
    #pd.DataFrame(creators_df).to_csv(os.environ["CREATORS"], index=False)

    return listed_creators

@dg.solid    # extract dimensions
def extract_dimensions(context,df):
    dimensions = df["dimensions"].str.extract(
        r"[.:] (?P<height>\d+,?\d?) [Xx] (?P<width>\d+,?\d?)"
    )
    df["image_width"] = dimensions["width"]
    df["image_height"] = dimensions["height"]

    catalog_df = df

    return catalog_df

 
@dg.composite_solid(output_defs=[dg.OutputDefinition(io_manager_key="df_csv")])
def catalog_main():
    root = read_xml()   
    outDict = find_uids(root)
    formated_table = fill_records(root,outDict)
    catalog_df = load(formated_table)
    catalog_df = rename_columns(catalog_df)
    catalog_df = select_columns(catalog_df)
    catalog_df = remove_extension(catalog_df)
    catalog_df = remove_duplicates(catalog_df)
    catalog_df = reverse_creators_name(catalog_df)
    catalog_df = dates_accuracy(catalog_df)
    catalog = extract_dimensions(catalog_df)
    listed_creators = creators_list(catalog_df)
    
    return catalog