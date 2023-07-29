import os
import shutil

def process_files(src_dir, dest_dir, infiles, process):
  for root, dirs, files in os.walk(src_dir):
    for file in files:
      if file in infiles:
        new_file_path = os.path.join(dest_dir, os.path.relpath(os.path.join(root, file), src_dir))
        if not os.path.exists(new_file_path):
          os.makedirs(os.path.dirname(new_file_path), exist_ok=True)
        #print("%s %s to %s" % (process, os.path.join(root, file), new_file_path))
        process(os.path.join(root, file), new_file_path)

copy_dirs = ['content', 'dlc']

def copy_rename_files(src_dir, dest_dir, infiles, outfiles):
    def copy_rename(src_file, dest_file):
       dest_dir = os.path.dirname(dest_file)
       src_basename = os.path.basename(src_file)
       dest_basename = outfiles[infiles.index(src_basename)]
       dest_file = dest_dir + '/' + dest_basename
       shutil.copy(src_file, dest_file)

    for subdir in copy_dirs:
        process_files(os.path.join(src_dir, subdir), os.path.join(dest_dir, subdir), infiles, copy_rename)

def copy_files(src_dir, dest_dir, infiles):
    for subdir in copy_dirs:
        process_files(os.path.join(src_dir, subdir), os.path.join(dest_dir, subdir), infiles, shutil.copy)

# convert w3strings files to csv files by w3strings.exe
def con2csv_files(src_dir, dest_dir, files):
  process_files(src_dir, dest_dir, files, lambda src, dest: os.system("w3strings.exe -d %s" % (src)))

def con2w3s_files(src_dir, dest_dir, files):
  process_files(src_dir, dest_dir, files, lambda src, dest: os.system("w3strings.exe -e %s --force-ignore-id-space-check-i-know-what-i-am-doing" % (src)))


# read two files of the following text format: 
# file #1: 
# ; this is comments of file #1
# id      |key(hex)|key(str)| text1_1<br>text1_2<br>text1_3
# ...
# file #2: 
# ; this is comments of file #2
# id      |key(hex)|key(str)| text2_1<br>text2_2<br>text2_3
# ...
# combine the text with the same id and key(hex) and save to the third file
# the combined format is as follows:
# file combined:
# ; comments from file #1
# id      |key(hex)|key(str)| text1_1<br>text2_1<br>text1_2<br>text2_2<br>text1_3<br>text2_3
# ...

def combine_file(src_file1, src_file2, dest_file):
    lines1 = read_file(src_file1)
    lines2 = read_file(src_file2)
    data1, comment1 = parse_lines(lines1)
    data2, _ = parse_lines(lines2)
    combined_data = combine_data(data1, data2)
    write_combined_file(dest_file, comment1, combined_data)
    
def read_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as file:
        lines = file.readlines()
    return lines

def parse_lines(lines):
    data = {}
    comments = []
    for line in lines:
        line = line.strip()
        if line.startswith(';'):
          comments.append(line)
          continue
        
        columns = line.split('|')
        id_value = columns[0].strip()
        key_hex = columns[1].strip()
        text = columns[3].strip()
        key = (id_value, key_hex)
        if key not in data:
            data[key] = []
        data[key].append(text)
        
    return data, comments

def combine_data(data1, data2):
    combined_data = {}
    for key, texts1 in data1.items():
        if key in data2:
            texts2 = data2[key]
            combined_data[key] = texts1 + texts2
    return combined_data

def write_combined_file(file_path, comments, combined_data):
    with open(file_path, 'w', encoding='utf-8') as file:
        if comments:
          for text in comments:
            file.write(text + '\n')
        # check which one is english
        en_inx = not "en" in comments

        for key, texts in combined_data.items():
            #if(key[1] != "00000000" or len(key[0]) >= 7):
            # if the last character of texts[1] is ".,?!" or texts[1] is longer than 32 characters,then insert <br>
            if texts[en_inx][-1] in ".,?!]…" or len(texts[en_inx]) > 32:
              combined_text = '<br>'.join(texts)
            else:
              if dialogue_only:
                combined_text = texts[0]
              else:
                combined_text = ' '.join(texts)

            file.write(f'{key[0].rjust(10)}|{key[1]}|| {combined_text}\n')

def combine_files(src_dir, dest_dir, infile1, infile2, outfile):
   process_files(src_dir, dest_dir, [infile1], 
                 lambda src, dest: combine_file(src, os.path.join(os.path.dirname(src), infile2), os.path.join(os.path.dirname(dest), outfile)))
   
backup_dir = "./backup"
working_dir = "./working"
witcher3_dir = "I:/SteamLibrary/steamapps/common/The Witcher 3"
install_dir = "./install"
#install_dir = witcher3_dir
installed_file = "dualsub.installed"
dialogue_only = False

if __name__ == "__main__":
  w3in_files = ["zh.w3strings", "en.w3strings"]
  w3out_file = "combined.csv"

  # check if installed file exists in install_dir
  if not os.path.exists(os.path.join(install_dir, installed_file)):
    print("backup files")
    copy_files(witcher3_dir, backup_dir, w3in_files)

  print("prepare working files")
  copy_files(backup_dir, working_dir, w3in_files)

  print("convert w3strings files to csv files")
  con2csv_files(working_dir, working_dir, w3in_files)

  print("combine files")
  combine_files(working_dir, working_dir, w3in_files[0]+'.csv', w3in_files[1]+'.csv', w3out_file)

  print("convert combined.csv to w3strings files")
  con2w3s_files(working_dir, working_dir, [w3out_file])


  # rename combined.csv.w3strings to zh.w3strings
  print("install files")
  copy_rename_files(working_dir, install_dir, [w3out_file+".w3strings"], [w3in_files[0]])

  # create a installed_file in install_dir
  with open(os.path.join(install_dir, installed_file), 'w', encoding='utf-8') as file:
     file.write("installed")

  #os.rename(os.path.join(working_dir, "content", "content0", "combined.w3strings.csv.w3strings"), 
  #          os.path.join(working_dir, "content", "content0", "1zh.w3strings"))  