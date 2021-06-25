import os
import sys
import time
import trio
import string
import random
import pickle
import shutil
import zipfile
import itertools
import subprocess
import pandas as pd
import infrastructure
from glob import glob
from copy import copy
from tqdm import tqdm
from pathlib import Path
sys.path.append('./crawlingathome-worker/')
from PIL import Image, ImageFile, UnidentifiedImageError

ImageFile.LOAD_TRUNCATED_IMAGES = True  # https://stackoverflow.com/a/47958486


def zipfolder(filename, target_dir):            
    zipobj = zipfile.ZipFile(filename, 'w', zipfile.ZIP_DEFLATED)
    rootlen = len(target_dir) + 1
    for base, dirs, files in os.walk(target_dir):
        for file in files:
            fn = os.path.join(base, file)
            zipobj.write(fn, fn[rootlen:])

def df_clipfilter(df):
    sim_threshold = 0.3
    underaged_text = ["teen", "kid", "child", "baby"]
    import clip_filter

    clip = clip_filter.CLIP()
    img_embedding, similarities = clip.preprocess_images(df)
    tmp_embed = copy(img_embedding)
    for i, img_embed in enumerate(tmp_embed):
        if similarities[i] < sim_threshold:
            df.drop(i, inplace=True)
            img_embedding.remove(img_embed)
            continue

        # get most similar categories
        nsfw_prob = clip.prob(img_embed, clip.categories)
        df.at[i, "NSFW"] = "UNSURE"
        df.at[i, "similarity"] = similarities[i]
        if nsfw_prob[0] < 19 and nsfw_prob[1] < 19:
            df.at[i, "NSFW"] = "UNLIKELY"
            continue
        elif nsfw_prob[0] >= 19 and nsfw_prob[1] >= 19:
            df.at[i, "NSFW"] = "NSFW"

        underage_prob = clip.prob(img_embed, clip.underaged_categories)
        if (
            underage_prob[0] < 4
            or underage_prob[1] < 4
            or any(x in df.at[i, "TEXT"] for x in underaged_text)
        ):
            df.drop(i, inplace=True)
            img_embedding.remove(img_embed)
            continue

        animal_prob = clip.prob(img_embed, clip.animal_categories)
        if animal_prob[0] > 20:
            df.drop(i, inplace=True)
            img_embedding.remove(img_embed)

    df.reset_index(drop=True, inplace=True)
    return df, img_embedding


def df_tfrecords(df, output_fname):
    import tensorflow as tf
    from tfr_image.utils import bytes_feature, int64_feature

    def image_to_tfexample(sample_id, image_data, image_format, height, width, caption):
        return tf.train.Example(
            features=tf.train.Features(
                feature={
                    "sampleID": bytes_feature(sample_id),
                    "image": bytes_feature(image_data),
                    "format": bytes_feature(image_format),
                    "label": bytes_feature(caption),
                    "height": int64_feature(height),
                    "width": int64_feature(width),
                }
            )
        )

    with tf.io.TFRecordWriter(output_fname) as tfrecord_writer:
        for i in range(len(df)):
            df_image = df.iloc[i]
            image_fname = df_image["PATH"]
            file_type = image_fname.split(".")[-1]
            with tf.io.gfile.GFile(image_fname, "rb") as f:
                image_data = f.read()
            example = image_to_tfexample(
                str(df_image["SAMPLE_ID"]).encode("utf_8"),
                image_data,
                file_type.encode("utf_8"),
                df_image["HEIGHT"],
                df_image["WIDTH"],
                df_image["TEXT"].encode("utf_8"),
            )
            tfrecord_writer.write(example.SerializeToString())

if __name__ == "__main__":
    output_folder = "./save/"
    csv_output_folder = output_folder
    img_output_folder = output_folder + "images/"

    YOUR_NICKNAME_FOR_THE_LEADERBOARD = os.getenv('CAH_NICKNAME')
    if YOUR_NICKNAME_FOR_THE_LEADERBOARD is None:
        YOUR_NICKNAME_FOR_THE_LEADERBOARD = "anonymous"
    CRAWLINGATHOME_SERVER_URL = "http://crawlingathome.duckdns.org/"

    print (f"[GPU] starting session under `{YOUR_NICKNAME_FOR_THE_LEADERBOARD}` nickname")

    nodes = sys.argv[1]
    location = None
    if len(sys.argv) == 2:
        location = sys.argv[2]
    workers = []

    try:
        start = time.time()
        # generate cloud workers
        workers = trio.run(infrastructure.up, nodes, location)
        trio.run(infrastructure.wait_for_infrastructure, workers)
        print(f"[infrastructure] {len(workers)} nodes cloud infrastructure is up and initialized in {round(time.time() - start)}s")
    except KeyboardInterrupt:
        print(f"[infrastructure] Abort! Deleting cloud infrastructure...")
        trio.run(infrastructure.down, nodes)
        print (f"[infrastructure] Cloud infrastructure was shutdown")
    except Exception as e:
        print(f"[infrastructure] Error, could not bring up infrastructure... please consider shutting down all workers via `python3 infrastructure.py down`")
        print (e)

    # poll for new GPU job
    for ip in itertools.cycle(workers): # make sure we cycle all workers
        try:
            print (f"[GPU] Checking {ip} node")
            print (f"[{ip}] " + infrastructure.last_status("crawl@"+ip, '/home/crawl/crawl.log').split("Downloaded:")[-1].rstrip())
            newjob = infrastructure.exists_remote("crawl@"+ip, "/home/crawl/semaphore", True)
            if not newjob:
                continue
            else:
                start = time.time()

                print (f"[{ip}] sending job to GPU")
                if os.path.exists(output_folder):
                    shutil.rmtree(output_folder)
                if os.path.exists(".tmp"):
                    shutil.rmtree(".tmp")

                os.mkdir(output_folder)
                os.mkdir(img_output_folder)
                os.mkdir(".tmp")

                start2 = time.time()

                # receive gpu job data (~500MB)
                subprocess.call(
                    ["scp", "-oIdentitiesOnly=yes", "-i~/.ssh/id_cah", "crawl@" + ip + ":" + "gpujob.zip", output_folder],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # delete file on remote so there is no secondary download
                subprocess.call(
                    ["ssh", "-oIdentitiesOnly=yes", "-i~/.ssh/id_cah", "crawl@" + ip, "rm -rf gpujob.zip"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.call(
                    ["ssh", "-oIdentitiesOnly=yes", "-i~/.ssh/id_cah", "crawl@" + ip, "rm -rf semaphore"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                with zipfile.ZipFile(output_folder+"gpujob.zip", 'r') as zip_ref:
                    zip_ref.extractall("./")
                os.remove(output_folder+"gpujob.zip")

                all_csv_files = []
                for path, subdir, files in os.walk(output_folder):
                    for file in glob(os.path.join(path, "*.csv")):
                        all_csv_files.append(file)

                # get name of csv file
                out_path = all_csv_files[0]
                out_fname = Path(out_path).stem.strip("_unfiltered").strip(".")
                print (out_fname)

                # recreate parsed dataset and run CLIP filtering
                dlparse_df = pd.read_csv(output_folder + out_fname + ".csv", sep="|")
                filtered_df, img_embeddings = df_clipfilter(dlparse_df)
                filtered_df.to_csv(output_folder + out_fname + ".csv", index=False, sep="|")
                
                img_embeds_sampleid = {}
                for i, img_embed_it in enumerate(img_embeddings):
                    dfid_index = filtered_df.at[i, "SAMPLE_ID"]
                    img_embeds_sampleid[str(dfid_index)] = img_embed_it
                with open(f"{output_folder}image_embedding_dict-{out_fname}.pkl", "wb") as f:
                    pickle.dump(img_embeds_sampleid, f)
                
                df_tfrecords(
                    filtered_df,
                    f"{output_folder}crawling_at_home_{out_fname}__00000-of-00001.tfrecord",
                )

                # clean img_output_folder now since we have all results do not want to transfer back all images...
                try:
                    shutil.rmtree(img_output_folder)
                    os.mkdir(img_output_folder)
                except OSError as e:
                    print("[GPU] Error deleting images: %s - %s." % (e.filename, e.strerror))

                # send GPU results
                subprocess.call(
                    ["zip", "-r", "gpujobdone.zip", output_folder],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.call(
                    ["touch", "gpusemaphore"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                subprocess.call(
                    ["scp", "-oIdentitiesOnly=yes", "-i~/.ssh/id_cah", "gpujobdone.zip", "crawl@"+ip + ":~/gpujobdone.zip"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                subprocess.call(
                    ["scp", "-oIdentitiesOnly=yes", "-i~/.ssh/id_cah", "gpusemaphore", "crawl@"+ip + ":~/gpusemaphore"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                os.remove("gpujobdone.zip")
                os.remove("gpusemaphore")

                print(f"[GPU] GPU job completed in {round(time.time() - start2)} seconds")
                print (f"[{ip}] resuming job with GPU results")
                
        except KeyboardInterrupt:
            print(f"[GPU] Abort! Deleting cloud infrastructure...")
            letters = string.ascii_lowercase
            suffix = ''.join(random.choice(letters) for i in range(3))
            for ip in workers:
                subprocess.call(
                        ["scp", "-oIdentitiesOnly=yes", "-i~/.ssh/id_cah", "crawl@" + ip + ":" + "crawl.log", ip + "_" + suffix + ".log"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            trio.run(infrastructure.down)
            print (f"[infrastructure] Cloud infrastructure was shutdown")
        
        except Exception as e:
            # todo shutdown and restart the offending ip
            print (f"[GPU] fault detected in job at worker-{ip}. Respawning offending worker...")
            print (e)
            workers = trio.run(infrastructure.respawn, workers, ip)
            continue
