# Steps

1 - Optional Step - ```uv venv```

2 -  Activate Virtual ENV : ```.venv\Scripts\activate```

3 - Install Requirements : ```uv pip install -r requirements.txt```

4 - `` cd .\FormEvaluation\`` -> ``uvicorn main:app --reload --port 8000``

5 - ``cd .\Form_Creation\`` -> ``uvicorn main:app --reload --port 8001``