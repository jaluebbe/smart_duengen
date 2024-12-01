#!/usr/bin/env python3
import tempfile
import os
import re
import zipfile
import json
from typing import Literal
from pydantic import BaseModel, Field
from fastapi import FastAPI, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
import uvicorn
import geopandas
from shapely.ops import unary_union


epsg_pattern = re.compile("^(?:EPSG|epsg):[0-9]{4,5}$")


class Point(BaseModel):
    type: Literal["Point"]
    coordinates: list[float]


class MultiPoint(BaseModel):
    type: Literal["MultiPoint"]
    coordinates: list[list[float]]


class LineString(BaseModel):
    type: Literal["LineString"]
    coordinates: list[list[float]]


class MultiLineString(BaseModel):
    type: Literal["MultiLineString"]
    coordinates: list[list[list[float]]]


class Polygon(BaseModel):
    type: Literal["Polygon"]
    coordinates: list[list[list[float]]]


class MultiPolygon(BaseModel):
    type: Literal["MultiPolygon"]
    coordinates: list[list[list[list[float]]]]


class Geometry(BaseModel):
    type: Literal[
        "Point",
        "MultiPoint",
        "LineString",
        "MultiLineString",
        "Polygon",
        "MultiPolygon",
    ]
    coordinates: list[float] | list[list[float]] | list[
        list[list[float]]
    ] | list[list[list[list[float]]]]


class Feature(BaseModel):
    type: Literal["Feature"]
    geometry: Geometry
    properties: dict = Field(default_factory=dict)


class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"]
    features: list[Feature]


class Settings(BaseModel):
    throwing_range: float = Field(default=15)
    min_speed: float = Field(default=1)
    default_rate: float = Field(default=0)
    default_speed: float = Field(default=2.2)


class ProjectFile(BaseModel):
    boundaries: FeatureCollection | None = None
    plan: FeatureCollection | None = None
    settings: Settings = Field(default_factory=Settings)


app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/static/gps_map_simple.html")


def shape_file_conversion(
    files: list[UploadFile], input_crs: str = "EPSG:4326"
):
    shapefiles = [
        file.filename
        for file in files
        if os.path.splitext(file.filename)[1].lower()
        in (".shp", ".zip", ".json", ".geojson")
    ]
    if len(shapefiles) == 0:
        raise HTTPException(status_code=404, detail="No shape data provided.")
    elif len(shapefiles) > 1:
        raise HTTPException(status_code=500, detail="Too many files.")
    with tempfile.TemporaryDirectory() as dir_name:
        for file in files:
            with open(os.path.join(dir_name, file.filename), "wb") as f:
                f.write(file.file.read())
        file_name = shapefiles[0]
        file_path = os.path.join(dir_name, file_name)
        if file_name.lower().endswith(".zip"):
            with zipfile.ZipFile(file_path) as z:
                print([_file.filename for _file in z.filelist])
                shp_in_zip = [
                    _file.filename
                    for _file in z.filelist
                    if _file.filename.lower().endswith(".shp")
                    and not _file.filename.startswith("__MACOSX/")
                    and not _file.filename.startswith("Rx/")
                ]
            if len(shp_in_zip) > 1:
                raise HTTPException(status_code=500, detail="Too many files.")
            myshpfile = geopandas.read_file(f"{file_path}!{shp_in_zip[0]}")
        else:
            myshpfile = geopandas.read_file(file_path)
        if myshpfile.crs is not None:
            original_crs = myshpfile.crs.srs
            if epsg_pattern.match(original_crs) is None:
                myshpfile = myshpfile.set_crs(input_crs)
        else:
            original_crs = None
            myshpfile = myshpfile.set_crs(input_crs)
        return {
            "file_name": os.path.splitext(file_name)[0],
            "geojson": json.loads(myshpfile.to_crs("EPSG:4326").to_json()),
            "input_crs": input_crs,
            "original_crs": original_crs,
        }


def process_plan_geojson(plan):
    all_properties = [
        _feature["properties"] for _feature in plan["geojson"]["features"]
    ]
    known_rate_keys = set(all_properties[0].keys()).intersection(
        ["RATE", "Menge", "rate", "fertilizer"]
    )
    if len(known_rate_keys) == 1:
        rate_key = known_rate_keys.pop()
    else:
        raise HTTPException(status_code=404, detail="No unique rate key found.")
    rate_values = [
        _property[rate_key]
        for _property in all_properties
        if _property[rate_key] > 0
    ]
    plan["min_rate"] = min(rate_values)
    max_rate = max(rate_values)
    plan["max_rate"] = max_rate
    for _property in all_properties:
        _property["V22RATE"] = _property[rate_key] / max_rate
    return plan


@app.post("/api/convert_plan_shape_files/")
async def convert_plan_shape_files(files: list[UploadFile]):
    plan_geojson = shape_file_conversion(files)
    return process_plan_geojson(plan_geojson)


def complete_project_file(project_file: ProjectFile):
    if project_file.boundaries is None:
        if project_file.plan is None:
            raise HTTPException(
                status_code=404, detail="No boundaries or plan provided."
            )
        gdf = geopandas.GeoDataFrame.from_features(
            [feature.model_dump() for feature in project_file.plan.features]
        )
        merged_polygons = unary_union(gdf.geometry)
        merged_gdf = geopandas.GeoDataFrame(
            geometry=[merged_polygons], crs=gdf.crs
        )
        boundaries_dict = json.loads(merged_gdf.to_json())
        features = [
            Feature(type="Feature", geometry=feature["geometry"])
            for feature in boundaries_dict["features"]
        ]
        project_file.boundaries = FeatureCollection(
            type="FeatureCollection", features=features
        )
    return project_file


@app.post("/api/create_project_file/")
async def create_project_file(project_file: ProjectFile):
    return complete_project_file(project_file)


@app.post("/api/convert_plan_shape_to_project/")
async def convert_plan_shape_to_project(files: list[UploadFile]):
    plan_geojson = shape_file_conversion(files)
    processed_plan = process_plan_geojson(plan_geojson)
    plan_feature_collection = FeatureCollection(**processed_plan["geojson"])
    project_file = ProjectFile(plan=plan_feature_collection)
    return complete_project_file(project_file)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
