import os
import shutil

def process_files(src_dir, dest_dir, process):
  for root, dirs, files in os.walk(src_dir):
    for file in files:
      if file in ["en.w3strings", "zh.w3strings"]:
        new_file_path = os.path.join(dest_dir, os.path.relpath(os.path.join(root, file), src_dir))
        if not os.path.exists(new_file_path):
          os.makedirs(os.path.dirname(new_file_path), exist_ok=True)
        print("%s %s to %s" % (process, os.path.join(root, file), new_file_path))
        process(os.path.join(root, file), new_file_path)

def copy_files(src_dir, dest_dir):
    process_files(os.path.join(src_dir, "content"), os.path.join(dest_dir, "content"), shutil.copyfile)
    process_files(os.path.join(src_dir, "dlc"), os.path.join(dest_dir, "dlc"), shutil.copyfile)

# convert w3strings files to csv files by w3strings.exe
def convert_files(src_dir, dest_dir):
  process_files(src_dir, dest_dir, lambda src, dest: os.system("w3strings.exe -d %s" % (src)))

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

def combine_files(src_file1, src_file2, dest_file):
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
        for key, texts in combined_data.items():
            combined_text = '<br>'.join(texts)
            file.write(f'{key[0].rjust(10)}|{key[1]}|| {combined_text}\n')


backup_dir = "./backup"
working_dir = "./working"
witcher3_dir = "./witcher3"
 
if __name__ == "__main__":
#  copy_files(witcher3_dir, backup_dir)
#  copy_files(witcher3_dir, working_dir)
#  convert_files(working_dir, working_dir)
  
  combine_files(os.path.join(working_dir, "content", "content0", "zh.w3strings.csv"), 
                os.path.join(working_dir, "content", "content0", "en.w3strings.csv"),
                os.path.join(working_dir, "content", "content0", "combined.w3strings.csv"),
                )
  os.system("w3strings.exe -e combined.w3strings.csv --force-ignore-id-space-check-i-know-what-i-am-doing")  
  
  