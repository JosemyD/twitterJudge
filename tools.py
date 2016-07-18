from __future__ import division

import json
import logging
import math
import os
import sys
import urlparse
from datetime import datetime

import numpy as np
import pymongo_spark
import requests
from dateutil import parser
from pyspark import SparkContext
from pyspark.conf import SparkConf
from pyspark.mllib.feature import HashingTF
from pyspark.mllib.regression import LabeledPoint
from pyspark.mllib.tree import RandomForest
from pyspark.sql import Row, HiveContext
from pyspark.sql.functions import udf, lag, length, collect_list, count, size, col
from pyspark.sql.window import Window

os.chdir(os.path.dirname(os.path.abspath(__file__)))
pymongo_spark.activate()

# logging.basicConfig(filename="logs/engine.log", format='%(levelname)s:%(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


def iniciar_spark_context(app_name=None, py_files=None):
    if not app_name:
        app_name = "ExtraerCaracteristicas"
    if not py_files:
        py_files = ['engine.py', 'app.py', 'tools.py']
    conf = SparkConf()
    conf.setAppName(app_name)
    sc = SparkContext(conf=conf, pyFiles=py_files)
    return sc


def hive_context(sc):
    return HiveContext(sc)


def quantize(signal, partitions, codebook):
    indices = []
    quanta = []
    for datum in signal:
        index = 0
        while index < len(partitions) and datum > partitions[index]:
            index += 1
        indices.append(index)
        quanta.append(codebook[index])
    return indices, quanta


def pattern_mat(x, m):
    """
    Construct a matrix of `m`-length segments of `x`.
    Parameters
    ----------
    x : (N, ) array_like
        Array of input data.
    m : int
        Length of segment. Must be at least 1. In the case that `m` is 1, the
        input array is returned.
    Returns
    -------
    patterns : (m, N-m+1)
        Matrix whose first column is the first `m` elements of `x`, the second
        column is `x[1:m+1]`, etc.
    Examples
    --------
    > p = pattern_mat([1, 2, 3, 4, 5, 6, 7], 3])
    array([[ 1.,  2.,  3.,	4.,	 5.],
           [ 2.,  3.,  4.,	5.,	 6.],
           [ 3.,  4.,  5.,	6.,	 7.]])
    """
    x = np.asarray(x).ravel()
    if m == 1:
        return x
    else:
        n = len(x)
        patterns = np.zeros((m, n - m + 1))
        for i in range(m):
            patterns[i, :] = x[i:n - m + i + 1]
        return patterns


def en_shannon(series, l, num_int):
    if not series:
        raise ValueError("No hay serie definida")
    if not l:
        raise ValueError("No hay dimension (L) definida")
    if not num_int:
        raise ValueError("num_int sin definir")
    # Normalizacion
    series = (series - np.mean(series)) / np.std(series)
    # We the values of the parameters required for the quantification:
    epsilon = (max(series) - min(series)) / num_int
    partition = np.arange(min(series), math.ceil(max(series)), epsilon)
    codebook = np.arange(-1, num_int + 1)
    # Uniform quantification of the time series:
    _, quants = quantize(series, partition, codebook)
    # The minimum value of the signal quantified assert passes -1 to 0:
    quants = [0 if x == -1 else x for x in quants]
    n = len(quants)
    # We compose the patterns of length 'L':
    X = pattern_mat(quants, l)
    # We get the number of repetitions of each pattern:
    num = np.ones(n - l + 1)
    # This loop goes over the columns of 'X':
    if l == 1:
        X = np.atleast_2d(X)
    for j in range(0, n - l + 1):
        for i2 in range(j + 1, n - l + 1):
            tmp = [0 if x == -1 else 1 for x in X[:, j]]
            if (tmp[0] == 1) and (X[:, j] == X[:, i2]).all():
                num[j] += 1
                X[:, i2] = -1
            tmp = -1

    # We get those patterns which are not NaN:
    aux = [0 if x == -1 else 1 for x in X[0, :]]
    # Now, we can compute the number of different patterns:
    new_num = []
    for j, a in enumerate(aux):
        if a != 0:
            new_num.append(num[j])
    new_num = np.asarray(new_num)

    # We get the number of patterns which have appeared only once:
    unique = sum(new_num[new_num == 1])
    # We compute the probability of each pattern:
    p_i = new_num / (n - l + 1)
    # Finally, the Shannon Entropy is computed as:
    SE = np.dot((- 1) * p_i, np.log(p_i))

    return SE, unique


def cond_en(series, l, num_int):
    if not series:
        raise ValueError("No hay serie definida")
    if not l:
        raise ValueError("No hay dimension (L) definida")
    if not num_int:
        raise ValueError("num_int sin definir")
    # Processing:
    # First, we call the Shannon Entropy function:
    # 'L' as embedding dimension:
    se, unique = en_shannon(series, l, num_int)
    # 'L-1' as embedding dimension:
    se_1, _ = en_shannon(series, l - 1, num_int)
    # The Conditional Entropy is defined as a differential entropy:
    ce = se - se_1
    return ce, unique


def correc_cond_en(series, lmax, num_int):
    if not series:
        raise ValueError("No hay serie definida")
    if not lmax:
        raise ValueError("No hay dimension (L) definida")
    if not num_int:
        raise ValueError("num_int sin definir")
    N = len(series)
    # We will use this for the correction term: (L=1)
    e_est_1, _ = en_shannon(series, 1, num_int)
    # Incializacin de la primera posicin del vector que almacena la CCE a un
    # numero elevado para evitar que se salga del bucle en L=2 (primera
    # iteracin):
    # CCE is a vector that will contian the several CCE values computed:
    CCE = sys.maxsize * np.ones(lmax + 1)
    CCE[0] = 100
    CE = np.ones(lmax + 1)
    uniques = np.ones(lmax + 1)
    correc_term = np.ones(lmax + 1)
    for L in range(2, lmax + 1):
        # First, we compute the CE for the current embedding dimension: ('L')
        CE[L], uniques[L] = cond_en(series, L, num_int)
        # Second, we compute the percentage of patterns which are not repeated:
        perc_l = uniques[L] / (N - L + 1)
        correc_term[L] = perc_l * e_est_1
        # Third, the CCE is the CE plus the correction term:
        CCE[L] = CE[L] + correc_term[L]

    # Finally, the best estimation of the CCE is the minimum value of all the
    # CCE that have been computed:
    cce_min = min(CCE)
    return cce_min


def lexical_diversity(text):
    if len(text) == 0:
        diversity = 0
    else:
        diversity = float(len(set(text))) / float(len(text))
    return diversity


def fuente(source):
    mobil = ["http://twitter.com/download/android", "Twitter for Android", "http://blackberry.com/twitter",
             "Twitter for BlackBerry", "https://mobile.twitter.com", "Mobile Web", "http://twitter.com/download/iphone",
             "iOS", "http://twitter.com/#!/download/ipad", "Huawei Social Phone", "Windows Phone",
             "Twitter for Nokia S40"]

    if "Twitter Web Client" in source:
        return 'uso_web'
    elif any(string in source for string in mobil):
        return 'uso_mobil'
    else:
        return 'uso_terceros'


def merge_two_dicts(x, y):
    """Given two dicts, merge them into a new dict as a shallow copy."""
    z = x.copy()
    z.update(y)
    return z


def porcentaje_dias(x):
    aux = check_dias(x[1])
    suma = 0
    for key, value in aux.items():
        suma += value
    for key, value in aux.items():
        aux[key] = value / suma

    return Row(user_id=x[0], **dict(aux))


def porcentaje_horas(x):
    aux = check_horas(x[1])
    suma = 0
    for key, value in aux.items():
        suma += value
    for key, value in aux.items():
        aux[key] = value / suma

    return Row(user_id=x[0], **dict(aux))


def porcentaje_fuentes(x):
    aux = check_fuentes(x[1])
    suma = 0
    for key, value in aux.items():
        suma += value
    for key, value in aux.items():
        aux[key] = value / suma

    return Row(user_id=x[0], **dict(aux))


def check_dias(x):
    if "Mon" not in x:
        x["Mon"] = 0
    if "Tue" not in x:
        x["Tue"] = 0
    if "Wed" not in x:
        x["Wed"] = 0
    if "Thu" not in x:
        x["Thu"] = 0
    if "Fri" not in x:
        x["Fri"] = 0
    if "Sat" not in x:
        x["Sat"] = 0
    if "Sun" not in x:
        x["Sun"] = 0
    return x


def check_horas(x):
    if "00" not in x:
        x["00"] = 0
    if "01" not in x:
        x["01"] = 0
    if "02" not in x:
        x["02"] = 0
    if "03" not in x:
        x["03"] = 0
    if "04" not in x:
        x["04"] = 0
    if "05" not in x:
        x["05"] = 0
    if "06" not in x:
        x["06"] = 0
    if "07" not in x:
        x["07"] = 0
    if "08" not in x:
        x["08"] = 0
    if "09" not in x:
        x["09"] = 0
    if "10" not in x:
        x["10"] = 0
    if "11" not in x:
        x["11"] = 0
    if "12" not in x:
        x["12"] = 0
    if "13" not in x:
        x["13"] = 0
    if "14" not in x:
        x["14"] = 0
    if "15" not in x:
        x["15"] = 0
    if "16" not in x:
        x["16"] = 0
    if "17" not in x:
        x["17"] = 0
    if "18" not in x:
        x["18"] = 0
    if "19" not in x:
        x["19"] = 0
    if "20" not in x:
        x["20"] = 0
    if "21" not in x:
        x["21"] = 0
    if "22" not in x:
        x["22"] = 0
    if "23" not in x:
        x["23"] = 0
    return x


def check_fuentes(x):
    if "uso_web" not in x:
        x["uso_web"] = 0
    if "uso_mobil" not in x:
        x["uso_mobil"] = 0
    if "uso_terceros" not in x:
        x["uso_terceros"] = 0
    return x


month_map = {
    'Jan': 1, 'Feb': 2, 'Mar': 3, 'Apr': 4, 'May': 5, 'Jun': 6, 'Jul': 7,
    'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12
}


def parse_time(s):
    return "{0:04d}-{1:02d}-{2:02d} {3:02d}:{4:02d}:{5:02d}".format(
        int(s[-4:]),
        month_map[s[4:7]],
        int(s[8:10]),
        int(s[11:13]),
        int(s[14:16]),
        int(s[17:19])
    )


def tweets_rdd(df):
    _tweets_rdd = df.map(lambda t: (t.user.id, (
        t.user.id,
        t.user.screen_name,
        t.id,
        t.text,
        t.entities,
        t.is_quote_status,
        t.in_reply_to_status_id,
        t.favorite_count,
        t.source,
        t.retweet_count,
        t.geo,
        t.lang,
        t.created_at,
        t.place,
    )))

    return _tweets_rdd


def tweets_x_dia(tweets):
    _tweets_x_dia = tweets.map(
        lambda t: ((t[0], parser.parse(t[1][12]).strftime('%a')), 1)).reduceByKey(
        lambda a, b: a + b).map(lambda t: (t[0][0], dict({t[0][1]: t[1]}))).reduceByKey(merge_two_dicts).map(
        porcentaje_dias).toDF().select(col("user_id"), col("Mon").alias("uso_lunes"),
                                       col("Tue").alias("uso_martes"),
                                       col("Wed").alias("uso_miercoles"), col("Thu").alias("uso_jueves"),
                                       col("Fri").alias("uso_viernes"), col("Sat").alias("uso_sabado"),
                                       col("Sun").alias("uso_domingo")).repartition("user_id")

    return _tweets_x_dia


def tweets_x_hora(tweets):
    _tweets_x_hora = tweets.map(
        lambda t: ((t[0], parser.parse(t[1][12]).strftime('%H')), 1)).reduceByKey(
        lambda a, b: a + b).map(lambda t: (t[0][0], dict({t[0][1]: t[1]}))).reduceByKey(merge_two_dicts).map(
        porcentaje_horas).toDF().select(col("user_id"), col("00").alias("hora_0"),
                                        col("01").alias("hora_1"),
                                        col("02").alias("hora_2"),
                                        col("03").alias("hora_3"),
                                        col("04").alias("hora_4"),
                                        col("05").alias("hora_5"),
                                        col("06").alias("hora_6"),
                                        col("07").alias("hora_7"),
                                        col("08").alias("hora_8"),
                                        col("09").alias("hora_9"),
                                        col("10").alias("hora_10"),
                                        col("11").alias("hora_11"),
                                        col("12").alias("hora_12"),
                                        col("13").alias("hora_13"),
                                        col("14").alias("hora_14"),
                                        col("15").alias("hora_15"),
                                        col("16").alias("hora_16"),
                                        col("17").alias("hora_17"),
                                        col("18").alias("hora_18"),
                                        col("19").alias("hora_19"),
                                        col("20").alias("hora_20"),
                                        col("21").alias("hora_21"),
                                        col("22").alias("hora_22"),
                                        col("23").alias("hora_23")).repartition("user_id")

    return _tweets_x_hora


def fuentes_usuario(tweets):
    _fuentes_usuario = tweets.map(lambda t: ((t[0], fuente(t[1][8])), 1)).reduceByKey(
        lambda a, b: a + b).map(lambda t: (t[0][0], dict({t[0][1]: t[1]}))).reduceByKey(merge_two_dicts).map(
        porcentaje_fuentes).toDF().repartition("user_id")

    return _fuentes_usuario


def avg_diversidad_lexicografica(tweets):
    _avg_diversidad_lexicografica = tweets.mapValues(lambda t: lexical_diversity(t[3])).combineByKey(
        lambda value: (value, 1), lambda x, value: (x[0] + value, x[1] + 1),
        lambda x, y: (x[0] + y[0], x[1] + y[1])).map(lambda label_value: Row(user_id=label_value[0],
                                                                             avg_diversidad_lex=float(
                                                                                 float(label_value[1][0]) / float(
                                                                                     label_value[1][
                                                                                         1])))).toDF().repartition(
        "user_id")

    return _avg_diversidad_lexicografica


def avg_long_tweets_x_usuario(tweets):
    _avg_long_tweets_x_usuario = tweets.mapValues(lambda t: len(t[3])).combineByKey(
        lambda value: (value, 1),
        lambda x, value: (x[0] + value, x[1] + 1),
        lambda x, y: (x[0] + y[0], x[1] + y[1])).map(lambda label_value: Row(user_id=label_value[0],
                                                                             avg_long_tweets=float(
                                                                                 float(label_value[1][0]) / float(
                                                                                     label_value[1][
                                                                                         1])))).toDF().repartition(
        "user_id")

    return _avg_long_tweets_x_usuario


def reply_ratio(tweets):
    _reply_ratio = tweets.mapValues(lambda t: 1 if t[6] is not None else 0).combineByKey(
        lambda value: (value, 1),
        lambda x, value: (x[0] + value, x[1] + 1),
        lambda x, y: (x[0] + y[0], x[1] + y[1])).map(lambda label_value: Row(user_id=label_value[0],
                                                                             reply_ratio=float(
                                                                                 float(label_value[1][0]) / float(
                                                                                     label_value[1][
                                                                                         1])))).toDF().repartition(
        "user_id")

    return _reply_ratio


def avg_hashtags(tweets):
    _avg_hashtags = tweets.mapValues(lambda t: len(t[4].hashtags)).combineByKey(lambda value: (value, 1),
                                                                                lambda x, value: (
                                                                                    x[0] + value, x[1] + 1),
                                                                                lambda x, y: (x[0] + y[0],
                                                                                              x[1] + y[
                                                                                                  1])).map(
        lambda label_value: Row(user_id=label_value[0], avg_hashtags=float(
            float(label_value[1][0]) / float(label_value[1][1])))).toDF().repartition("user_id")

    return _avg_hashtags


def mention_ratio(tweets):
    _mention_ratio = tweets.mapValues(lambda t: len(t[4].user_mentions)).combineByKey(
        lambda value: (value, 1), lambda x, value: (x[0] + value, x[1] + 1),
        lambda x, y: (x[0] + y[0], x[1] + y[1])).map(lambda label_value: Row(user_id=label_value[0],
                                                                             mention_ratio=float(
                                                                                 float(label_value[1][0]) / float(
                                                                                     label_value[1][
                                                                                         1])))).toDF().repartition(
        "user_id")

    return _mention_ratio


def avg_palabras(tweets):
    _avg_palabras = tweets.mapValues(lambda t: len(t[3].split(" "))).combineByKey(lambda value: (value, 1),
                                                                                  lambda x, value: (
                                                                                      x[0] + value,
                                                                                      x[1] + 1),
                                                                                  lambda x, y: (x[0] + y[0],
                                                                                                x[1] + y[
                                                                                                    1])).map(
        lambda label_value: Row(user_id=label_value[0], avg_palabras=float(
            float(label_value[1][0]) / float(label_value[1][1])))).toDF().repartition("user_id")

    return _avg_palabras


def avg_diversidad(tweets):
    _avg_diversidad = tweets.mapValues(
        lambda t: len(set(t[3].split(" "))) / len(t[3].split(" "))).combineByKey(lambda value: (value, 1),
                                                                                 lambda x, value: (
                                                                                     x[0] + value, x[1] + 1),
                                                                                 lambda x, y: (
                                                                                     x[0] + y[0], x[1] + y[1])).map(
        lambda label_value: Row(user_id=label_value[0], avg_diversidad=float(
            float(label_value[1][0]) / float(label_value[1][1])))).toDF().repartition("user_id")

    return _avg_diversidad


def df_url_ratio(df):
    return df.groupby(df.user.id).agg((sum(size(df.entities.urls)) / count(df.text)).alias("url_ratio"))


def url_ratio(tweets):
    _url_ratio = tweets.mapValues(lambda t: len(t[4].urls)).combineByKey(lambda value: (value, 1),
                                                                         lambda x, value: (
                                                                             x[0] + value, x[1] + 1),
                                                                         lambda x, y: (
                                                                             x[0] + y[0], x[1] + y[1])).map(
        lambda label_value: Row(user_id=label_value[0], url_ratio=float(
            float(label_value[1][0]) / float(label_value[1][1])))).toDF().repartition("user_id")

    return _url_ratio


def avg_spam(juez, tweets):
    tf = HashingTF(numFeatures=200)

    text_tweets = tweets.mapValues(lambda tweet: Row(features=tf.transform(tweet[3].split(" "))))

    predictions = juez.predict(text_tweets.map(lambda t: t[1].features))

    ids_predictions = text_tweets.map(lambda t: t[0]).zip(predictions)

    _avg_spam = ids_predictions.combineByKey(lambda value: (value, 1), lambda x, value: (x[0] + value, x[1] + 1),
                                             lambda x, y: (x[0] + y[0], x[1] + y[1])).map(
        lambda label_value: Row(user_id=label_value[0],
                                avg_spam=float(
                                    float(label_value[1][0]) / float(label_value[1][1])))).toDF().repartition("user_id")

    return _avg_spam


def preparar_df(df):
    df.repartition(df.user.id)

    df = df.where(length(df.text) > 0)
    u_parse_time = udf(parse_time)
    df = df.select("*", u_parse_time(df['created_at']).cast('timestamp').alias('created_at_ts'))

    df_intertweet = df.select(df.user.id.alias("user_id"), (
        df.created_at_ts.cast('bigint') - lag(df.created_at_ts.cast('bigint'), ).over(
            Window.partitionBy("user.id").orderBy("created_at_ts"))).cast("bigint").alias("time_intertweet"))

    df_list_intertweet = df_intertweet.groupby(df_intertweet.user_id).agg(
        collect_list("time_intertweet").alias("lista_intertweet"))

    df = df.join(df_list_intertweet, df["user.id"] == df_list_intertweet["user_id"])

    return df


def get_final_url(url):
    try:
        resultado = requests.get(url, timeout=10)
        if resultado.status_code >= 400:
            return str(resultado.status_code)
        else:
            return urlparse.urldefrag(resultado.url)[0]
    except Exception:
        return "600"


def diversidad_urls(urls):
    resultado_urls = []
    hosts = []
    diversidad_url = 0
    n_urls = 0
    logger.debug("Iniciando proceso para %s urls", str(len(urls)))
    for _url in urls:
        for url in _url:
            logger.debug("Obteniendo redireccion final de url %s", str(url["expanded_url"]))
            finalurl = get_final_url(url["expanded_url"])
            logger.debug("ANTES: %s RESULTADO: %s", str(url["expanded_url"]), str(finalurl))
            if not finalurl.isdigit():
                resultado_urls.append(finalurl)
                hosts.append(urlparse.urlparse(finalurl).netloc)
                n_urls += 1
    if n_urls != 0:
        diversidad_url = len(set(hosts)) / n_urls
    return diversidad_url


def intertweet_urls(directorio):
    lista_intertweet = []
    lista_urls = []
    i = 0
    with open(directorio) as timeline:
        lines = timeline.readlines()
        if json.loads(lines[0]) and len(lines) > 100:
            while i + 1 != len(lines) and i < 110:
                tweet = (json.loads(lines[i]), json.loads(lines[i + 1]))
                i += 1
                date = (parser.parse(tweet[0]['created_at']), parser.parse(tweet[1]['created_at']))
                lista_intertweet.append(abs((date[1] - date[0]).total_seconds()))
                if tweet[0]['entities']['urls'][0]:
                    lista_urls.append(tweet[0]['entities']['urls'])
    return json.dumps(
        dict(intertweet_delay=lista_intertweet, user_id=json.loads(lines[0])["user"]["id"], urls=lista_urls))


def entropia_urls(directorio, urls=False):
    data = intertweet_urls(directorio)
    entropia = correc_cond_en(data["intertweet_delay"], len(data["intertweet_delay"]),
                              int(np.ceil(np.log2(max(data["intertweet_delay"])))))
    if urls:
        diversidad = diversidad_urls(data["urls"])
        return entropia, diversidad
    return entropia


def tweets_features(_tweets_rdd, juez):
    logger.info("Calculando features para tweets...")

    logger.info("Iniciando calculo de tweets por dia...")

    _tweets_x_dia = tweets_x_dia(_tweets_rdd)

    logger.info("Iniciando calculo de tweets por hora...")

    _tweets_x_hora = tweets_x_hora(_tweets_rdd)
    acumulador = _tweets_x_dia.join(_tweets_x_hora, _tweets_x_dia.user_id == _tweets_x_hora.user_id)\
        .drop(_tweets_x_hora.user_id)

    logger.info("Iniciando exploracion de las fuentes de los tweets...")

    _fuentes_usuario = fuentes_usuario(_tweets_rdd)
    acumulador = acumulador.join(_fuentes_usuario, _fuentes_usuario.user_id == acumulador.user_id)\
        .drop(acumulador.user_id)

    logger.info("Iniciando calculo de diversidad lexicografica...")

    _avg_diversidad_lexicografica = avg_diversidad_lexicografica(_tweets_rdd)
    acumulador = acumulador.join(_avg_diversidad_lexicografica,
                                 _avg_diversidad_lexicografica.user_id == acumulador.user_id).drop(acumulador.user_id)

    logger.info("Iniciando calculo del promedio de la longuitud de los tweets...")

    _avg_long_tweets_x_usuario = avg_long_tweets_x_usuario(_tweets_rdd)
    acumulador = acumulador.join(_avg_long_tweets_x_usuario, _avg_long_tweets_x_usuario.user_id == acumulador.user_id)\
        .drop(acumulador.user_id)

    logger.info("Iniciando calculo del ratio de respuestas...")

    _reply_ratio = reply_ratio(_tweets_rdd)
    acumulador = acumulador.join(_reply_ratio, _reply_ratio.user_id == acumulador.user_id).drop(acumulador.user_id)

    logger.info("Iniciando calculo del promedio de los hashtags...")

    _avg_hashtags = avg_hashtags(_tweets_rdd)
    acumulador = acumulador.join(_avg_hashtags, _avg_hashtags.user_id == acumulador.user_id).drop(acumulador.user_id)

    logger.info("Iniciando calculo del promedio de menciones...")

    _mention_ratio = mention_ratio(_tweets_rdd)
    acumulador = acumulador.join(_mention_ratio, _mention_ratio.user_id == acumulador.user_id).drop(acumulador.user_id)

    logger.info("Iniciando calculo del promedio de palabras por tweet...")

    _avg_palabras = avg_palabras(_tweets_rdd)
    acumulador = acumulador.join(_avg_palabras, _avg_palabras.user_id == acumulador.user_id).drop(acumulador.user_id)

    logger.info("Iniciando calculo del promedio de diversidad de palabras...")

    _avg_diversidad = avg_diversidad(_tweets_rdd)
    acumulador = acumulador.join(_avg_diversidad, _avg_diversidad.user_id == acumulador.user_id)\
        .drop(acumulador.user_id)

    logger.info("Iniciando calculo del ratio de urls...")

    _url_ratio = url_ratio(_tweets_rdd)
    acumulador = acumulador.join(_url_ratio, _url_ratio.user_id == acumulador.user_id).drop(acumulador.user_id)

    logger.info("Iniciando calculo del avg de tweets SPAM...")

    _avg_spam = avg_spam(juez, _tweets_rdd)
    resultado = acumulador.join(_avg_spam, _avg_spam.user_id == acumulador.user_id).drop(acumulador.user_id)

    return resultado


def usuarios_features(df, categoria=-1):
    logger.info("Calculando features para usuarios...")
    _usuarios_features = df.map(lambda t: Row(
        user_id=t.user.id,
        con_imagen_fondo=1 if t.user.profile_use_background_image else 0,
        ano_registro=int(parser.parse(t.user.created_at).strftime('%Y')),
        n_favoritos=t.user.favourites_count,
        con_descripcion=1 if len(t.user.description) else 0,
        con_perfil_verificado=1 if t.user.verified else 0,
        con_imagen_default=1 if t.user.default_profile_image else 0,
        n_listas=t.user.listed_count,
        con_geo_activo=1 if t.user.geo_enabled else 0,
        reputacion=t.user.followers_count / (
            t.user.followers_count + t.user.friends_count) if t.user.followers_count or t.user.friends_count or (
            t.user.followers_count + t.user.friends_count > 0) else 0,
        n_tweets=t.user.statuses_count,
        followers_ratio=t.user.followers_count / t.user.friends_count if t.user.friends_count > 0 else 0,
        entropia=float(correc_cond_en(t.lista_intertweet[:110], len(t.lista_intertweet[:110]),
                                      int(np.ceil(
                                          np.log2(max(t.lista_intertweet[:110])))))),
        nombre_usuario=t.user.screen_name,
        categoria=categoria)).toDF()

    return _usuarios_features


def entrenar_spam(sc, sql_context, dir_spam, dir_no_spam, num_trees=3, max_depth=2):
    input_spam = sc.textFile(dir_spam)
    input_no_spam = sc.textFile(dir_no_spam)

    spam = sql_context.jsonRDD(input_spam).map(lambda t: t.text)
    no_spam = sql_context.jsonRDD(input_no_spam).map(lambda t: t.text)

    tf = HashingTF(numFeatures=200)

    spam_features = spam.map(lambda tweet: tf.transform(tweet.split(" ")))
    no_spam_features = no_spam.map(lambda tweet: tf.transform(tweet.split(" ")))

    ejemplos_spam = spam_features.map(lambda features: LabeledPoint(1, features))
    ejemplos_no_spam = no_spam_features.map(lambda features: LabeledPoint(0, features))

    training_data = ejemplos_spam.union(ejemplos_no_spam)
    training_data.cache()

    modelo = RandomForest.trainClassifier(training_data, numClasses=2, categoricalFeaturesInfo={}, numTrees=num_trees,
                                          featureSubsetStrategy="auto", impurity='gini', maxDepth=max_depth, maxBins=32)

    return modelo


def cargar_datos(sc, sql_context, directorio):
    timeline = sc.textFile(directorio)
    logger.info("Cargando arhcivos...")
    df = sql_context.jsonRDD(timeline)
    df = preparar_df(df)
    return df


# TODO agregar features faltantes (safety, diversidad url)
def entrenar_juez(sc, sql_context, juez_spam, mongo_uri, directorio, num_trees=10, max_depth=5):
    df_humanos = cargar_datos(sc, sql_context, directorio["humanos"])
    df_bots = cargar_datos(sc, sql_context, directorio["bots"])
    df_ciborgs = cargar_datos(sc, sql_context, directorio["ciborgs"])

    tweets_rdd_humanos = tweets_rdd(df_humanos)
    tweets_rdd_bots = tweets_rdd(df_bots)
    tweets_rdd_ciborgs = tweets_rdd(df_ciborgs)

    _tweets_rdd = sc.union([tweets_rdd_bots, tweets_rdd_ciborgs, tweets_rdd_humanos])

    df_humanos = df_humanos.dropDuplicates(["user.id"])
    df_bots = df_bots.dropDuplicates(["user.id"])
    df_ciborgs = df_ciborgs.dropDuplicates(["user.id"])

    tweets = tweets_features(_tweets_rdd, juez_spam)

    usuarios_features_humanos = usuarios_features(df_humanos, 0)
    usuarios_features_ciborgs = usuarios_features(df_bots, 1)
    usuarios_features_bots = usuarios_features(df_ciborgs, 2)

    usuarios = usuarios_features_ciborgs.unionAll(usuarios_features_bots)
    usuarios = usuarios.unionAll(usuarios_features_humanos)
    usuarios.cache()

    set_datos = usuarios.join(tweets, tweets.user_id == usuarios.user_id).cache()

    labeled_point = set_datos.map(
        lambda t: LabeledPoint(t.categoria,
                               [
                                   t.ano_registro,
                                   t.con_descripcion,
                                   t.con_geo_activo,
                                   t.con_imagen_default,
                                   t.con_imagen_fondo,
                                   t.con_perfil_verificado,
                                   t.followers_ratio,
                                   t.n_favoritos,
                                   t.n_listas,
                                   t.n_tweets,
                                   t.reputacion,
                                   t.url_ratio,
                                   t.avg_diversidad,
                                   t.avg_palabras,
                                   t.mention_ratio,
                                   t.avg_hashtags,
                                   t.reply_ratio,
                                   t.avg_long_tweets,
                                   t.avg_diversidad_lex,
                                   t.uso_lunes,
                                   t.uso_martes,
                                   t.uso_miercoles,
                                   t.uso_jueves,
                                   t.uso_viernes,
                                   t.uso_sabado,
                                   t.uso_domingo,
                                   t.hora_0,
                                   t.hora_1,
                                   t.hora_2,
                                   t.hora_3,
                                   t.hora_4,
                                   t.hora_5,
                                   t.hora_6,
                                   t.hora_7,
                                   t.hora_8,
                                   t.hora_9,
                                   t.hora_10,
                                   t.hora_11,
                                   t.hora_12,
                                   t.hora_13,
                                   t.hora_14,
                                   t.hora_15,
                                   t.hora_16,
                                   t.hora_17,
                                   t.hora_18,
                                   t.hora_19,
                                   t.hora_20,
                                   t.hora_21,
                                   t.hora_22,
                                   t.hora_23,
                                   t.uso_web,
                                   t.uso_mobil,
                                   t.uso_terceros,
                                   t.entropia,
                                   0,
                                   t.avg_spam,
                                   0
                               ])).cache()

    modelo = RandomForest.trainClassifier(labeled_point, numClasses=3, categoricalFeaturesInfo={}, numTrees=num_trees,
                                          featureSubsetStrategy="auto", impurity='gini', maxDepth=max_depth, maxBins=32)

    set_datos.map(lambda t: t.asDict()).saveToMongoDB(mongo_uri)

    return modelo


def join_tw_usr(tw_features, usr_features):
    set_datos = usr_features.join(tw_features, tw_features.user_id == usr_features.user_id).map(
        lambda t: (Row(user_id=t.user_id,
                       ano_registro=t.ano_registro,
                       con_descripcion=t.con_descripcion,
                       con_geo_activo=t.con_geo_activo,
                       con_imagen_default=t.con_imagen_default,
                       con_imagen_fondo=t.con_imagen_fondo,
                       con_perfil_verificado=t.con_perfil_verificado,
                       followers_ratio=t.followers_ratio,
                       n_favoritos=t.n_favoritos,
                       n_listas=t.n_listas,
                       n_tweets=t.n_tweets,
                       reputacion=t.reputacion,
                       url_ratio=t.url_ratio,
                       avg_diversidad=t.avg_diversidad,
                       avg_palabras=t.avg_palabras,
                       mention_ratio=t.mention_ratio,
                       avg_hashtags=t.avg_hashtags,
                       reply_ratio=t.reply_ratio,
                       avg_long_tweets=t.avg_long_tweets,
                       avg_diversidad_lex=t.avg_diversidad_lex,
                       uso_lunes=t.uso_lunes,
                       uso_martes=t.uso_martes,
                       uso_miercoles=t.uso_miercoles,
                       uso_jueves=t.uso_jueves,
                       uso_viernes=t.uso_viernes,
                       uso_sabado=t.uso_sabado,
                       uso_domingo=t.uso_domingo,
                       hora_0=t.hora_0,
                       hora_1=t.hora_1,
                       hora_2=t.hora_2,
                       hora_3=t.hora_3,
                       hora_4=t.hora_4,
                       hora_5=t.hora_5,
                       hora_6=t.hora_6,
                       hora_7=t.hora_7,
                       hora_8=t.hora_8,
                       hora_9=t.hora_9,
                       hora_10=t.hora_10,
                       hora_11=t.hora_11,
                       hora_12=t.hora_12,
                       hora_13=t.hora_13,
                       hora_14=t.hora_14,
                       hora_15=t.hora_15,
                       hora_16=t.hora_16,
                       hora_17=t.hora_17,
                       hora_18=t.hora_18,
                       hora_19=t.hora_19,
                       hora_20=t.hora_20,
                       hora_21=t.hora_21,
                       hora_22=t.hora_22,
                       hora_23=t.hora_23,
                       uso_web=t.uso_web,
                       uso_mobil=t.uso_mobil,
                       uso_terceros=t.uso_terceros,
                       entropia=t.entropia,  # Entropia
                       diversidad_url=0,  # Diversidad
                       avg_spam=t.avg_spam,  # SPAM or not SPAM
                       safety_url=0,  # Safety url
                       createdAt=datetime.utcnow(),
                       nombre_usuario=t.nombre_usuario)))
    return set_datos


def timeline_features(juez_spam, df):
    _tweets_rdd = tweets_rdd(df)
    _tweets_features = tweets_features(_tweets_rdd, juez_spam)
    df = df.dropDuplicates(["user.id"])
    _usuarios_features = usuarios_features(df)
    logger.info("Realizando join de usuarios con tweets...")
    set_datos = join_tw_usr(_tweets_features, _usuarios_features)
    logger.info("Finalizado el join...")

    return set_datos


def predecir(juez_usuario, features):
    predicciones = juez_usuario.predict(features.map(lambda t: (t.ano_registro,
                                                                t.con_descripcion,
                                                                t.con_geo_activo,
                                                                t.con_imagen_default,
                                                                t.con_imagen_fondo,
                                                                t.con_perfil_verificado,
                                                                t.followers_ratio,
                                                                t.n_favoritos,
                                                                t.n_listas,
                                                                t.n_tweets,
                                                                t.reputacion,
                                                                t.url_ratio,
                                                                t.avg_diversidad,
                                                                t.avg_palabras,
                                                                t.mention_ratio,
                                                                t.avg_hashtags,
                                                                t.reply_ratio,
                                                                t.avg_long_tweets,
                                                                t.avg_diversidad_lex,
                                                                t.uso_lunes,
                                                                t.uso_martes,
                                                                t.uso_miercoles,
                                                                t.uso_jueves,
                                                                t.uso_viernes,
                                                                t.uso_sabado,
                                                                t.uso_domingo,
                                                                t.hora_0,
                                                                t.hora_1,
                                                                t.hora_2,
                                                                t.hora_3,
                                                                t.hora_4,
                                                                t.hora_5,
                                                                t.hora_6,
                                                                t.hora_7,
                                                                t.hora_8,
                                                                t.hora_9,
                                                                t.hora_10,
                                                                t.hora_11,
                                                                t.hora_12,
                                                                t.hora_13,
                                                                t.hora_14,
                                                                t.hora_15,
                                                                t.hora_16,
                                                                t.hora_17,
                                                                t.hora_18,
                                                                t.hora_19,
                                                                t.hora_20,
                                                                t.hora_21,
                                                                t.hora_22,
                                                                t.hora_23,
                                                                t.uso_web,
                                                                t.uso_mobil,
                                                                t.uso_terceros,
                                                                t.entropia,
                                                                t.diversidad_url,
                                                                t.avg_spam,
                                                                t.safety_url)))
    return predicciones


def evaluar(sc, sql_context, juez_spam, juez_usuario, dir_timeline, mongo_uri):
    df = cargar_datos(sc, sql_context, dir_timeline)
    features = timeline_features(juez_spam, df).cache()
    predicciones = predecir(juez_usuario, features)
    features = features.zip(predicciones).map(lambda t: dict(t[0].asDict().items() + [("prediccion", t[1])])).cache()
    features.saveToMongoDB(mongo_uri)

    return features.map(lambda t: t["user_id"]).collect()
