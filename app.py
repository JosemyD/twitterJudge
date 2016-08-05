import json
import logging
import os

from flask import Blueprint
from flask import Flask, request

import engine

os.chdir(os.path.dirname(os.path.abspath(__file__)))

main = Blueprint('main', __name__)

logging.basicConfig(filename="logs/engine.log", format='%(levelname)s:%(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


@main.route("/entrenar_juez/", methods=["POST"])
def entrenar_juez():
    """
    Realiza la carga del set de entrenamiento y genera el juez.
    Requiere de la especificacion de los directorios para las 3 categorias.
    Returns
    -------
    resultado : bool
        Booleano que sera True en caso de ejecutarse exitosamente
    Examples
    --------
    > curl -H "Content-Type: application/json" -X POST -d
        '{"bots":"/carpeta/con/bots","humanos":"/carpeta/con/humanos","ciborgs":"/carpeta/con/ciborg"}'
         http://[host]:[port]/entrenar_juez/
    """
    logger.debug("Iniciando carga inicial...")
    data = request.json
    logging.info(data)
    if "bots" not in data:
        logging.error("No se especifico la direccion de la carpeta para los bots")
        return json.dumps(dict(resultado=False))
    if "humanos" not in data:
        logging.error("No se especifico la direccion de la carpeta para los humanos")
        return json.dumps(dict(resultado=False))
    if "ciborgs" not in data:
        logging.error("No se especifico la direccion de la carpeta para los ciborgs")
        return json.dumps(dict(resultado=False))
    if "num_trees" not in data:
        logging.warn("No se especifico numero de arboles, se utilizaran 3 por defecto")
    if "max_depth" not in data:
        logging.warn("No se especifico profundidad del bosque, se utilizara 2 por defecto")
    logger.debug("Ejecutando carga y entrenamiento")
    resultado = motor_clasificador.entrenar_juez(data.get("humanos"), data.get("ciborgs"), data.get("bots"),
                                                 data.get("num_trees", 3), data.get("max_depth", 2))
    logger.debug("Finalizando carga y entrenamiento")
    return json.dumps(dict(resultado=resultado))


@main.route("/entrenar_spam/", methods=["POST"])
def entrenar_spam():
    """Realiza la carga del set de entrenamiento y genera el juez.
    Requiere de la especificacion de los directorios para las 2 categorias SPAM y NoSPAM.
    Returns
    -------
    resultado : bool
        Booleano que sera True en caso de ejecutarse exitosamente
    Examples
    --------
    > curl -H "Content-Type: application/json" -X POST -d
    '{"spam":"/archivo/spam","no_spam":"/archivo/no_spam"}'
    http://[host]:[port]/entrenar_spam/
    > curl -H "Content-Type: application/json" -X POST -d
    '{"spam":"/archivo/spam","no_spam":"/archivo/no_spam"}, "num_trees":3, "max_depth":2'
    http://[host]:[port]/entrenar_spam/
    """
    logger.debug("Iniciando carga...")
    data = request.json
    logging.info(data)
    if "spam" not in data:
        logging.error("No se especifico la direccion del archivo de SPAM")
        return json.dumps(dict(resultado=False))
    if "no_spam" not in data:
        logging.error("No se especifico la direccion del archivo de NOSPAM")
        return json.dumps(dict(resultado=False))
    if "num_trees" not in data:
        logging.warn("No se especifico numero de arboles, se utilizaran 3 por defecto")
    if "max_depth" not in data:
        logging.warn("No se especifico profundidad del bosque, se utilizara 2 por defecto")
    logger.debug("Ejecutando carga y entrenamiento")
    resultado = motor_clasificador.entrenar_spam(data["spam"], data["no_spam"], data.get("num_trees", 3),
                                                 data.get("max_depth", 2))
    logger.debug("Finalizando carga y entrenamiento")
    return json.dumps(dict(resultado=resultado))


@main.route("/evaluar/", methods=["POST"])
def evaluar():
    """
    Realiza la evaluacion de los timelines.
    Requiere de la especificacion del directorio que contiene los timelines
    Returns
    -------
    resultado : diccionario
        Sera False, en caso de error. Contendra el id de los usuarios evaluados.
    Examples
    --------
    > curl -H "Content-Type: application/json" -X POST -d
    '{"directorio":"/carpeta/con/timelines/*"}'
    http://[host]:[port]/evaluar/
    """
    if not request.json.get("directorio"):
        logging.error("No se especifico el parametro 'directorio' para evaluar")
        return json.dumps(dict(resultado=False))
    directorio = request.json.get("directorio")
    logger.info("Iniciando evaluacion sobre: %s", directorio)
    resultado = motor_clasificador.evaluar(directorio)
    return json.dumps(dict(resultado=resultado))


@main.route("/alive/", methods=["GET"])
def alive():
    """Funcion para verificar disponibilidad del servidor"""
    return json.dumps(dict(resultado="I'm Alive!"))


def create_app():
    global motor_clasificador
    motor_clasificador = engine.MotorClasificador()
    app = Flask(__name__)
    app.register_blueprint(main)
    return app
